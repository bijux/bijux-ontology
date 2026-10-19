"""Finite before/after consumer transitions over unchanged native reasoning APIs."""

from collections.abc import Iterator
from threading import Event

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)

from ...axioms.codec.encoding import ConstructionEncoder
from ...backend.types import OwlProfileBackend
from ...documents.types import OwlDocument
from ...inference.consistency.checking import check_consistency
from ...inference.entailment.checking import check_entailments
from ...inference.explanations.checking import explain_reasoning
from ...inference.explanations.types import ExplanationQuestion
from ...inference.model_contracts.checking import check_model_compatibility
from ...inference.relationships.checking import query_inferred_contracts
from ...inference.relationships.types import InferredContracts, InferredQuery
from .preparation import CapturedSide, capture_side
from .types import (
    EntailmentContract,
    LogicalObservation,
    LogicalSide,
    LogicalSnapshot,
    LogicalTransition,
    LogicalUpgrade,
    SatisfiabilityContract,
    SeparationContract,
    TransitionKind,
    UpgradeExplanation,
)


def _separated(
    contract: SeparationContract, raw: InferredContracts, operation: Operation
) -> bool | None:
    """Use actual equivalence nodes only within the explicitly selected inventory."""
    if raw.consistent is not True:
        return None
    size = sum(len(group) for group in contract.groups)
    operation.consume("states", size)
    operation.consume("bytes", 128 + 128 * size)
    positions = {
        value: index for index, group in enumerate(contract.groups) for value in group
    }
    complete = len(raw.answers) == size
    for answer in raw.answers:
        operation.consume("work")
        if answer.native_error is not None:
            complete = False
            continue
        origin = positions[answer.query.focus]
        complete = complete and answer.report.complete
        for group in answer.groups:
            complete = complete and group.complete
            for member in group.members:
                operation.consume("work")
                other = positions.get(member.value)
                if other is not None and other != origin:
                    return False
    return True if complete else None


def _queries(
    contract: SeparationContract, operation: Operation
) -> tuple[InferredQuery, ...]:
    """Prepay every exact selected equivalence request before materializing it."""
    size = sum(len(group) for group in contract.groups)
    operation.consume("states", size)
    operation.consume("bytes", 64 + size * 128)
    return tuple(
        InferredQuery(value, "equivalent")
        for group in contract.groups
        for value in group
    )


def _observations(
    backend: OwlProfileBackend,
    side: CapturedSide,
    operation: Operation,
    heap_mb: int,
    reports: list[Report],
) -> Iterator[LogicalObservation]:
    """Reuse existing qualified methods; satisfiability never substitutes entailment."""
    size = len(side.consumer.satisfiability)
    operation.consume("bytes", 512 + 256 * size)
    operation.consume("states", size + 2)
    classes = tuple(URIRef(item.class_iri) for item in side.consumer.satisfiability)
    raw = check_consistency(
        backend,
        side.source,
        named_classes=classes,
        imports=side.imports,
        budget=operation,
        heap_mb=heap_mb,
    )
    reports.append(raw.report)
    yield LogicalObservation("ontology-consistency", raw.consistent, raw)
    for item in side.consumer.satisfiability:
        passed = None
        if raw.consistent is True:
            for answer in raw.classes:
                operation.consume("work")
                if str(answer.iri) == item.class_iri and answer.report.complete:
                    passed = answer.satisfiable
                    break
        yield LogicalObservation(item.identifier, passed, raw)
    for entailed in side.consumer.entailments:
        operation.consume("bytes", 512)
        operation.consume("states", 2)
        proof = check_entailments(
            backend,
            side.source,
            (entailed.axiom,),
            imports=side.imports,
            budget=operation,
            heap_mb=heap_mb,
        )
        reports.append(proof.report)
        passed = None
        if proof.consistent is True and len(proof.answers) == 1:
            entailed_answer = proof.answers[0]
            if entailed_answer.report.complete and entailed_answer.native_error is None:
                passed = entailed_answer.raw_entailed
        yield LogicalObservation(entailed.identifier, passed, proof)
    for model in side.consumer.models:
        operation.consume("bytes", 512)
        operation.consume("states", 2)
        compatible = check_model_compatibility(
            backend,
            side.source,
            model.models,
            imports=side.imports,
            budget=operation,
            heap_mb=heap_mb,
        )
        reports.append(compatible.report)
        passed = None
        if compatible.consistent is True:
            if any(issue.kind == "finding" for issue in compatible.report.issues):
                passed = False
            elif compatible.report.verified:
                passed = True
        yield LogicalObservation(model.identifier, passed, compatible)
    for separate in side.consumer.separate:
        operation.consume("bytes", 512)
        operation.consume("states", 2)
        relations = query_inferred_contracts(
            backend,
            side.source,
            _queries(separate, operation),
            imports=side.imports,
            budget=operation,
            heap_mb=heap_mb,
        )
        reports.append(relations.report)
        yield LogicalObservation(
            separate.identifier, _separated(separate, relations, operation), relations
        )


def _kind(before: bool | None, after: bool | None) -> TransitionKind:
    """Classify established states without inferring truth from acceptance policy."""
    if before is None or after is None:
        return "unknown"
    if before and not after:
        return "regression"
    if not before and not after:
        return "persistent_prior_failure"
    return "improvement" if not before else "preserved"


def _question(
    identifier: str,
    before: CapturedSide | None,
    after: CapturedSide,
    transition: LogicalTransition,
) -> tuple[CapturedSide, ExplanationQuestion] | None:
    """Select only a real positive observation for maintained support generation."""
    if transition.kind != "regression":
        return None
    if identifier == "ontology-consistency":
        return after, ExplanationQuestion("inconsistency")
    contract = after.contracts[identifier]
    if isinstance(contract, SatisfiabilityContract):
        return after, ExplanationQuestion(
            "unsatisfiability", class_iri=contract.class_iri, location=contract.location
        )
    if isinstance(contract, EntailmentContract) and before is not None:
        old = before.contracts.get(identifier)
        if isinstance(old, EntailmentContract):
            return before, ExplanationQuestion(
                "entailment", old.axiom, location=old.location
            )
    return None


def assess_logical_upgrade(
    backend: OwlProfileBackend,
    before: OwlDocument | None,
    after: OwlDocument,
    *,
    before_consumer: LogicalSnapshot | None,
    after_consumer: LogicalSnapshot,
    before_imports: dict[str, OwlDocument] | None = None,
    after_imports: dict[str, OwlDocument] | None = None,
    explain: tuple[str, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> LogicalUpgrade:
    """Assess only explicit finite consumer snapshots, never ontology equivalence.

    Both native worlds and all application metadata are owned before callbacks.
    Stable IDs match obligations; changed definitions remain visible. Raw child
    reports retain prior failures, independently of the typed impact verdict.
    Optional selected explanations qualify support separately from the observed
    regression; failure to explain never erases an established transition.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    old = new = None
    old_observations: list[LogicalObservation] = []
    new_observations: list[LogicalObservation] = []
    old_index: dict[str, LogicalObservation] = {}
    transitions: list[LogicalTransition] = []
    explanations: list[UpgradeExplanation] = []
    children: list[Report] = []
    adopted: set[int] = set()
    selected: tuple[str, ...] = ()
    expected = 1
    finished = False
    try:
        if (before is None) != (before_consumer is None):
            raise InputError("A baseline requires both native and consumer snapshots")
        if before is None and before_imports is not None:
            raise InputError("Baseline imports require a baseline document")
        if before is not None and before_consumer is not None:
            old = capture_side(before, before_consumer, before_imports, operation)
        new = capture_side(after, after_consumer, after_imports, operation)
        encoder = ConstructionEncoder(operation)
        values = encoder.sequence(explain)
        selected_list: list[str] = []
        for value in values:
            if type(value) is not str:
                raise InputError("Explanation selections require stable consumer IDs")
            value = encoder.text(value)
            if value in selected_list:
                raise InputError("Explanation IDs must be unique")
            selected_list.append(value)
        selected = tuple(selected_list)
        inventory = 1 + len(new.contracts)
        # Reserve every publication/reference before any child can pay a finding.
        size = inventory + (0 if old is None else 1 + len(old.contracts))
        operation.consume("work", 16 * size + len(selected) * inventory)
        operation.consume("states", 8 * size + 4 * len(selected))
        operation.consume("bytes", 2048 + 2048 * size + 1024 * len(selected))
        expected = inventory + len(selected)
        for current, observations in ((old, old_observations), (new, new_observations)):
            if current is None:
                continue
            for observation in _observations(
                backend, current, operation, heap_mb, children
            ):
                observations.append(observation)
                report = observation.check.report
                if current is old:
                    old_index[observation.identifier] = observation
                else:
                    previous = old_index.get(observation.identifier)
                    kind = _kind(
                        None if previous is None else previous.passed,
                        observation.passed,
                    )
                    changed = (
                        None
                        if old is None or previous is None
                        else (
                            False
                            if observation.identifier == "ontology-consistency"
                            else old.fingerprints[observation.identifier]
                            != new.fingerprints[observation.identifier]
                        )
                    )
                    transition = LogicalTransition(
                        observation.identifier, previous, observation, kind, changed
                    )
                    transitions.append(transition)
                    if kind == "regression":
                        issues.append(
                            Issue(
                                "owl.upgrade_regression",
                                "An established consumer obligation newly fails",
                                observation.identifier,
                                kind="finding",
                            )
                        )
                    elif kind == "unknown":
                        issues.append(
                            Issue(
                                "owl.upgrade_unverified",
                                "Matched before/after evidence is not established",
                                observation.identifier,
                                kind="unverified",
                            )
                        )
                if id(report) not in adopted:
                    adopted.add(id(report))
                    retain_report(report, operation, issues)
        # Removed consumer IDs are explicit unmatched evidence, never silently lost.
        if old is not None:
            for identifier, observation in old_index.items():
                if (
                    identifier != "ontology-consistency"
                    and identifier not in new.contracts
                ):
                    operation.consume("bytes", 512)
                    operation.consume("states")
                    transitions.append(
                        LogicalTransition(
                            identifier, observation, None, "unknown", None
                        )
                    )
                    expected += 1
                    issues.append(
                        Issue(
                            "owl.upgrade_unmatched_consumer",
                            "The after snapshot omits a baseline consumer ID",
                            identifier,
                            kind="unverified",
                        )
                    )
        for identifier in selected:
            selected_transition = next(
                (item for item in transitions if item.identifier == identifier), None
            )
            question = (
                None
                if selected_transition is None
                else _question(identifier, old, new, selected_transition)
            )
            if question is None:
                issues.append(
                    Issue(
                        "owl.upgrade_explanation_unverified",
                        "No supported positive observation supplies the explanation",
                        identifier,
                        kind="unverified",
                    )
                )
                continue
            owner, request = question
            raw = explain_reasoning(
                backend,
                owner.source,
                request,
                imports=owner.imports,
                budget=operation,
                heap_mb=heap_mb,
            )
            children.append(raw.report)
            explanations.append(
                UpgradeExplanation(
                    identifier, "before" if owner is old else "after", raw
                )
            )
            retain_report(raw.report, operation, issues)
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "owl.upgrade_unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "owl.upgrade_input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped))
    for report in children:
        issues.retain_completed(report)
    checked = sum(item.kind != "unknown" for item in transitions)
    checked += sum(item.check.explanation_report.verified for item in explanations)
    complete = (
        finished and checked == expected and all(child.complete for child in children)
    )
    regression = any(item.kind == "regression" for item in transitions)
    compatible = False if regression else True if complete else None

    def side_result(
        captured: CapturedSide | None, observations: list[LogicalObservation]
    ) -> LogicalSide | None:
        """Publish prepaid input and result references, including partial work."""
        if captured is None:
            return None
        return LogicalSide(
            captured.source,
            tuple((captured.imports or {}).items()),
            captured.consumer,
            tuple(observations),
        )

    report = Report(
        "owl.logical_upgrade",
        checked=checked,
        requested=max(expected, checked),
        complete=complete,
        regime="owl-dl-consumer-transitions",
        issues=tuple(issues),
        children=tuple(children),
    )
    return LogicalUpgrade(
        side_result(old, old_observations),
        side_result(new, new_observations),
        tuple(transitions),
        tuple(explanations),
        compatible,
        report,
    )
