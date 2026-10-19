"""Actual remaining-theory proofs and joint removal through native transactions."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from threading import Event

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)

from ...axioms.changes.checking import prepare_axiom_change
from ...axioms.changes.preconditions import source_base
from ...axioms.changes.types import AxiomCandidate, AxiomCheck
from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import ClassRelation, ProjectedAxiom, SubClassOf
from ...backend.types import OwlProfileBackend
from ...documents.sources.capture import capture_document, capture_imports
from ...documents.types import OwlDocument
from ...inference.consistency.checking import check_consistency
from ...inference.entailment.checking import check_entailments
from ...inference.entailment.encoding import encode_axiom
from ...inference.entailment.types import EntailmentCheck
from .types import RedundancyAnalysis, RedundancyCheck


def _policies(annotations: str, imports: str, operation: Operation) -> None:
    """Require explicit bounded policies before interpreting caller metadata."""
    for value in (annotations, imports):
        if type(value) is not str:
            raise InputError("Reduction policies require ordinary strings")
        bound_text(value, operation)
    if annotations != "retain" or imports != "read_only":
        raise InputError("Choose retain annotations and read_only imports")


def _removals(
    values: Sequence[ProjectedAxiom], operation: Operation
) -> tuple[ProjectedAxiom, ...]:
    """Keep annotations and imported assertions outside the editable selection."""
    selected, _ = ConstructionEncoder(operation).axioms(values)
    if not selected:
        raise InputError("Select nonempty removable root logical assertions")
    operation.consume("work", len(selected))
    operation.consume("states", len(selected))
    operation.consume("bytes", 128 + 64 * len(selected))
    seen: set[ProjectedAxiom] = set()
    for axiom in selected:
        if axiom.annotations:
            raise InputError("Reduction preserves annotated assertions unchanged")
        _queries((axiom,), operation)
        if axiom in seen:
            raise InputError("Removable assertions must be distinct")
        seen.add(axiom)
    return selected


def _queries(
    axioms: tuple[ProjectedAxiom, ...], operation: Operation
) -> tuple[ProjectedAxiom, ...]:
    """Reduce named equivalence obligations to maintained bidirectional queries.

    Other requests use the existing finite query profile directly. This expands
    a selected OWL equivalence axiom without constructing a new semantic engine.
    """
    count = sum(
        2 * (len(axiom.classes) - 1)
        if type(axiom) is ClassRelation and axiom.kind == "equivalent"
        else 1
        for axiom in axioms
    )
    operation.consume("work", count)
    operation.consume("states", count * 4)
    operation.consume("bytes", 128 + count * 256)
    result: list[ProjectedAxiom] = []
    for axiom in axioms:
        if type(axiom) is ClassRelation and axiom.kind == "equivalent":
            if len(axiom.classes) < 2 or axiom.annotations:
                raise InputError("Equivalent-class proofs require unannotated members")
            first = axiom.classes[0]
            for other in axiom.classes[1:]:
                result.extend(
                    (SubClassOf(first, other, ()), SubClassOf(other, first, ()))
                )
        else:
            result.append(axiom)
    for query in result:
        encode_axiom(query, operation)
    return tuple(result)


def _error(issues: IssueCollector, error: InputError | OSError) -> None:
    """Keep invalid requests, unavailable execution and exhausted work distinct."""
    try:
        unknown = isinstance(error, ResourceError | OSError)
        issues.append(
            Issue(
                "owl.reduction_unverified" if unknown else "owl.reduction_input_error",
                str(error),
                kind="unverified" if unknown else "input_error",
            )
        )
    except LimitExceeded as stopped:
        issues.exhausted(str(stopped), location="owl.reduction")


def _prepare(
    backend: OwlProfileBackend,
    original: OwlDocument,
    removals: tuple[ProjectedAxiom, ...],
    obligations: tuple[ProjectedAxiom, ...],
    imports: Mapping[str, OwlDocument] | None,
    operation: Operation,
    heap_mb: int,
    proofs: list[EntailmentCheck],
) -> AxiomCandidate:
    """Require every removed assertion and consumer proof in the joint candidate."""
    operation.consume("states", 4 + len(removals) + len(obligations))
    operation.consume("bytes", 512 + 64 * (len(removals) + len(obligations)))
    queries = _queries(removals + obligations, operation)

    def check(candidate: OwlDocument, budget: Budget) -> Report:
        """Buffer raw paid evidence before the transaction adopts its report."""
        proof = check_entailments(
            backend,
            candidate,
            queries,
            imports=imports,
            budget=budget,
            heap_mb=heap_mb,
        )
        proofs.append(proof)
        return proof.report

    return prepare_axiom_change(
        backend,
        original,
        expected=source_base(original),
        removals=removals,
        imports=imports,
        profiles=("owl2-dl",),
        after_checks=(AxiomCheck("retained logical consequences", check),),
        budget=operation,
        heap_mb=heap_mb,
    )


def prepare_reduction(
    backend: OwlProfileBackend,
    source: OwlDocument,
    removals: Sequence[ProjectedAxiom],
    *,
    consumer_entailments: Sequence[ProjectedAxiom],
    imports: Mapping[str, OwlDocument] | None = None,
    annotation_policy: str,
    import_policy: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> AxiomCandidate:
    """Check an explicitly accepted joint removal against the remaining theory.

    Only unannotated supported logical root assertions may be removed. Imports
    and every other annotation stay unchanged. Both the original and remaining
    theory must be consistent; all removed assertions and selected nonempty
    consumer obligations must be entailed by the one joint result. Publish only
    its verified sealed bytes through ordinary write_axiom_change acceptance.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = None
    baseline = None
    result = None
    proofs: list[EntailmentCheck] = []
    try:
        _policies(annotation_policy, import_policy, operation)
        original = capture_document(source, operation)
        supplied = capture_imports(imports, operation)
        selected = _removals(removals, operation)
        obligations, _ = ConstructionEncoder(operation).axioms(consumer_entailments)
        if not obligations:
            raise InputError("Select nonempty consumer entailments for reduction")
        operation.consume("states", 8)
        operation.consume("bytes", 1024)
        baseline = check_consistency(
            backend,
            original,
            named_classes=(),
            imports=supplied,
            budget=operation,
            heap_mb=heap_mb,
        )
        retain_report(baseline.report, operation, issues)
        if baseline.consistent is not True or not baseline.report.verified:
            raise ResourceError(
                "An unqualified original theory cannot prove redundancy"
            )
        result = _prepare(
            backend,
            original,
            selected,
            obligations,
            supplied,
            operation,
            heap_mb,
            proofs,
        )
        retain_report(result.report, operation, issues)
        return replace(result, before=(baseline.report, *result.before))
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.reduction")
    except (InputError, OSError) as error:
        _error(issues, error)
    if baseline is not None:
        issues.retain_completed(baseline.report)
    if result is not None:
        issues.retain_completed(result.report)
    for proof in proofs:
        issues.retain_completed(proof.report)
    if result is not None:
        return replace(
            result,
            report=Report(
                "owl.reduction",
                issues=tuple(issues),
                requested=1,
                complete=False,
                children=(result.report,),
            ),
            before=() if baseline is None else (baseline.report,),
        )
    return AxiomCandidate(
        None,
        source if original is None else original,
        (),
        (),
        (),
        () if baseline is None else (baseline.report,),
        (),
        None,
        Report("owl.reduction", issues=tuple(issues), requested=1, complete=False),
    )


def analyze_redundancy(
    backend: OwlProfileBackend,
    source: OwlDocument,
    removable: Sequence[ProjectedAxiom],
    *,
    annotation_policy: str,
    import_policy: str,
    imports: Mapping[str, OwlDocument] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> RedundancyAnalysis:
    """Test each selection independently; recheck accepted removals jointly.

    Proposals never imply their union is safe. Nonredundant selections keep raw
    negative proof findings; unknown or inconsistent work cannot propose removal.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    original = None
    checks: list[RedundancyCheck] = []
    reports: list[Report] = []
    selected: tuple[ProjectedAxiom, ...] = ()
    try:
        _policies(annotation_policy, import_policy, operation)
        original = capture_document(source, operation)
        supplied = capture_imports(imports, operation)
        selected = _removals(removable, operation)
        operation.consume("states", len(selected) * 4 + 8)
        operation.consume("bytes", 512 + 512 * len(selected))
        for axiom in selected:
            # Independent candidates deliberately start from the same original.
            candidate = prepare_reduction(
                backend,
                original,
                (axiom,),
                consumer_entailments=(axiom,),
                imports=supplied,
                annotation_policy="retain",
                import_policy="read_only",
                budget=operation,
                heap_mb=heap_mb,
            )
            reports.append(candidate.report)
            redundant = True if candidate.report.verified else None
            if (
                candidate.report.complete
                and candidate.report.outcome == "disagreement"
                and candidate.after
            ):
                redundant = False
            checks.append(RedundancyCheck(axiom, redundant, candidate))
            retain_report(candidate.report, operation, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.redundancy")
    except (InputError, OSError) as error:
        _error(issues, error)
    for report in reports:
        issues.retain_completed(report)
    return RedundancyAnalysis(
        original,
        tuple(checks),
        tuple(check.axiom for check in checks if check.redundant is True),
        Report(
            "owl.redundancy",
            sum(check.redundant is not None for check in checks),
            tuple(issues),
            requested=max(1, len(selected)),
            complete=bool(selected)
            and len(checks) == len(selected)
            and all(check.redundant is not None for check in checks),
            children=tuple(reports),
            regime="selected-original-root-counterfactuals",
        ),
    )
