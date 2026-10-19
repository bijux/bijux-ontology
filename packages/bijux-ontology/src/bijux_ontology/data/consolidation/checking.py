"""Explicit repeated-description agreement and required candidate postconditions."""

import json
import threading
from collections.abc import Sequence

from rdflib import Dataset, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.codecs import term_data
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import PatchBlank, PatchLiteral, PatchValue
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_term, term_key
from bijux_ontology.rdf.transforms import merge_sources
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values

from .indexing import describe_snapshots, qualify_snapshot
from .types import (
    Consolidation,
    ConsolidationCheck,
    CorpusContribution,
    CorpusIndex,
    CorpusSnapshot,
    EntityDescription,
    PredicateAgreement,
)


class _CallbackFailure(Exception):
    """Keep deliberate caller exceptions outside owned input/error conversion."""

    def __init__(self, original: Exception) -> None:
        """Retain the actual exception without formatting its arbitrary message."""
        self.original = original


def _literal(value: PatchLiteral) -> Literal:
    """Reuse existing exact lexical construction for a qualified original term."""
    return literal_term(value.lexical, datatype=value.datatype, language=value.language)


def _token(value: PatchValue, operation: Operation) -> tuple[str | None, ...]:
    """Use established RDF identity, retaining document-local anonymous tokens."""
    term_data(value, operation)
    if isinstance(value, PatchBlank):
        return "blank", value.token
    return term_key(_literal(value) if isinstance(value, PatchLiteral) else value)


class _Agreement:
    """Retain bounded original contributions beside policy-specific conclusions."""

    def __init__(self, operation: Operation, issues: IssueCollector) -> None:
        """Share the caller's cumulative comparison and evidence allowances."""
        self.operation, self.issues = operation, issues
        self.checked = 0
        self.children: list[Report] = []

    def evidence(
        self, entity: EntityDescription, contributions: Sequence[CorpusContribution]
    ) -> str:
        """Reserve all retained origin/term fields before canonical JSON allocation."""
        rows: list[dict[str, object]] = []
        size = 512
        self.operation.consume("states", len(contributions))
        for contribution in contributions:
            self.operation.consume("work")
            origin, quad = contribution.origin, contribution.quad
            for text in (origin.identifier, origin.document_iri, origin.effective_url):
                if text is not None:
                    bound_text(text, self.operation)
                    size += 12 * len(text)
            terms = [
                term_data(value, self.operation)
                for value in (quad.subject, quad.predicate, quad.value, quad.graph)
            ]
            for term in terms:
                size += 128 + 12 * sum(
                    len(part) for part in term if isinstance(part, str)
                )
            self.operation.consume("bytes", 128)
            rows.append(
                {
                    "document": origin.identifier,
                    "document_iri": origin.document_iri,
                    "effective_url": origin.effective_url,
                    "quad": terms,
                }
            )
        entity_terms = (
            term_data(entity.subject, self.operation),
            term_data(entity.graph, self.operation),
        )
        size += 128 + 12 * sum(
            len(part) for term in entity_terms for part in term if isinstance(part, str)
        )
        if size > self.operation.limits.max_bytes:
            raise LimitExceeded("Consolidation evidence exceeds max_bytes")
        self.operation.consume("work", size)
        self.operation.consume("bytes", size)
        encoded = json.dumps(
            {
                "subject": term_data(entity.subject, self.operation),
                "graph": term_data(entity.graph, self.operation),
                "contributions": rows,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(encoded) > self.operation.limits.max_bytes:
            raise LimitExceeded("Consolidation evidence exceeds max_bytes")
        return encoded

    def issue(
        self,
        code: str,
        message: str,
        entity: EntityDescription,
        contributions: Sequence[CorpusContribution],
        *,
        unknown: bool = False,
    ) -> None:
        """Keep application findings separate from logical inconsistency claims."""
        self.issues.append(
            Issue(
                code,
                message,
                kind="unverified" if unknown else "finding",
                evidence_json=self.evidence(entity, contributions),
            )
        )

    def equal(
        self, left: PatchValue, right: PatchValue, policy: PredicateAgreement
    ) -> bool | None:
        """Compare exact references or qualified original literal value spaces."""
        if (
            policy.equality == "rdf-term"
            or not isinstance(left, PatchLiteral)
            or not isinstance(right, PatchLiteral)
        ):
            if isinstance(left, PatchBlank) or isinstance(right, PatchBlank):
                return None
            return _token(left, self.operation) == _token(right, self.operation)
        result = compare_literal_values(
            _literal(left),
            _literal(right),
            regime=policy.equality,
            budget=self.operation,
        )
        retain_report(result.report, self.operation, self.issues)
        self.operation.consume("states")
        self.children.append(result.report)
        return (
            True
            if result.relation == "equal"
            else False
            if result.relation == "different"
            else None
        )

    def entity(
        self,
        entity: EntityDescription,
        policies: tuple[PredicateAgreement, ...],
        additive: frozenset[URIRef],
    ) -> None:
        """Check only repeated named descriptions in their actual selected scope."""
        if not isinstance(entity.subject, URIRef):
            return
        documents: dict[str, list[CorpusContribution]] = {}
        for contribution in entity.contributions:
            self.operation.consume("work")
            self.operation.consume("states")
            self.operation.consume("bytes", 16)
            documents.setdefault(contribution.origin.identifier, []).append(
                contribution
            )
        if len(documents) < 2:
            return
        self.operation.consume("states", len(policies))
        self.operation.consume("work", len(policies))
        self.operation.consume("bytes", 64 + 32 * len(policies))
        policy_map = {policy.predicate: policy for policy in policies}
        self.operation.consume("states", 2 * len(policies))
        self.operation.consume("bytes", 48 * len(policies))
        predicates = list(policy_map)
        seen_predicates = set(policy_map)
        for contribution in entity.contributions:
            self.operation.consume("work")
            predicate = contribution.quad.predicate
            if predicate not in seen_predicates:
                self.operation.consume("states", 2)
                self.operation.consume("bytes", 48)
                predicates.append(predicate)
                seen_predicates.add(predicate)
        for predicate in predicates:
            self.operation.consume("work")
            policy = policy_map.get(predicate)
            if predicate in additive:
                self.checked += 1
                continue
            values: list[tuple[PatchValue, CorpusContribution]] = []
            token_sets = []
            missing = False
            multiple = False
            for contributions in documents.values():
                self.operation.consume("states")
                selected: dict[
                    tuple[str | None, ...], tuple[PatchValue, CorpusContribution]
                ] = {}
                for contribution in contributions:
                    self.operation.consume("work")
                    if contribution.quad.predicate == predicate:
                        token = _token(contribution.quad.value, self.operation)
                        self.operation.consume("states")
                        self.operation.consume("bytes", 64)
                        selected[token] = contribution.quad.value, contribution
                self.operation.consume("states", len(selected))
                self.operation.consume("bytes", 32 * len(selected))
                token_sets.append(set(selected))
                if not selected:
                    missing = True
                if len(selected) > 1:
                    multiple = True
                self.operation.consume("states", len(selected))
                values.extend(selected.values())
            self.checked += 1
            if policy is None:
                same = True
                for tokens in token_sets[1:]:
                    self.operation.consume(
                        "work",
                        1 + sum(len(part or "") for token in tokens for part in token),
                    )
                    same = same and tokens == token_sets[0]
                if not same:
                    self.issue(
                        "data.consolidation_unverified",
                        "Different descriptions add facts without an explicit "
                        "additive or agreement policy",
                        entity,
                        entity.contributions,
                        unknown=True,
                    )
                continue
            if multiple or missing and policy.required:
                self.issue(
                    "data.consolidation_conflict",
                    "Repeated entity descriptions violate the selected "
                    "single-valued required agreement",
                    entity,
                    entity.contributions,
                )
                continue
            for index in range(1, len(values)):
                self.operation.consume("work")
                equal = self.equal(values[0][0], values[index][0], policy)
                if equal is not True:
                    self.issue(
                        "data.consolidation_unverified"
                        if equal is None
                        else "data.consolidation_conflict",
                        "Selected predicate values could not establish agreement"
                        if equal is None
                        else "Declared immutable values disagree across documents",
                        entity,
                        (values[0][1], values[index][1]),
                        unknown=equal is None,
                    )


def _owned(snapshot: CorpusSnapshot, operation: Operation) -> PreparedSource:
    """Materialize only immutable authorized participation through the RDF seam."""
    qualify_snapshot(snapshot, operation)
    metadata = PreparedSource(
        Dataset(),
        is_dataset=False,
        document_iri=snapshot.origin.document_iri,
        effective_url=snapshot.origin.effective_url,
        literal_fidelity=snapshot.literal_fidelity,
    )
    return _materialize(snapshot.witness, metadata, operation)


def consolidate_entities(
    index: CorpusIndex,
    *,
    agreements: Sequence[PredicateAgreement] = (),
    additive_predicates: Sequence[URIRef] = (),
    union: bool = False,
    checks: Sequence[ConsolidationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> Consolidation:
    """Publish only complete application agreement and selected postcondition truth.

    Graph partitions remain separate unless union=True explicitly requests the
    effective graph for ordinary model/SHACL/logical callbacks. Input bases are
    already established; no imports or external resources are fetched. Trusted
    callbacks run once on independent snapshots and cannot be forcibly preempted.
    Multiple types/references are never classified as OWL contradictions here.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    state = _Agreement(operation, issues)
    source = None
    merged_report = None
    post_reports: list[Report] = []
    children: list[Report] = []
    try:
        if not isinstance(index, CorpusIndex) or type(union) is not bool:
            raise InputError(
                "Consolidation requires an owned index and boolean union intent"
            )
        retain_report(index.report, operation, issues)
        children.append(index.report)
        snapshots = capture_items(index.snapshots, CorpusSnapshot, operation)
        if index.report.checked != len(snapshots) or index.report.requested != len(
            snapshots
        ):
            issues.append(
                Issue(
                    "data.consolidation_unverified",
                    "Corpus snapshot inventory omits required document scope",
                    kind="unverified",
                )
            )
        names: set[str] = set()
        for snapshot in snapshots:
            qualify_snapshot(snapshot, operation)
            if snapshot.origin.identifier in names:
                raise InputError("Corpus document identifiers must remain distinct")
            operation.consume("states")
            names.add(snapshot.origin.identifier)
        policies = capture_items(agreements, PredicateAgreement, operation)
        additive = capture_items(additive_predicates, URIRef, operation)
        required = capture_items(checks, ConsolidationCheck, operation)
        seen_checks: set[str] = set()
        for declaration in required:
            bound_text(declaration.name, operation)
            if (
                not declaration.name
                or declaration.name in seen_checks
                or not callable(declaration.check)
            ):
                raise InputError("Postconditions require distinct names and callbacks")
            operation.consume("states")
            seen_checks.add(declaration.name)
        seen_predicates: set[URIRef] = set()
        for policy in policies:
            bound_node(policy.predicate, operation)
            bound_text(policy.equality, operation)
            if (
                policy.predicate in seen_predicates
                or policy.equality
                not in ("rdf-term", "sparql-1.1", "owl2-datatype-map")
                or type(policy.required) is not bool
            ):
                raise InputError(
                    "Agreement predicates require distinct explicit policies"
                )
            operation.consume("states")
            seen_predicates.add(policy.predicate)
        for predicate in additive:
            bound_node(predicate, operation)
            if predicate in seen_predicates:
                raise InputError("Additive predicates cannot also require agreement")
        operation.consume("states", len(additive))
        operation.consume("bytes", 64 + 32 * len(additive))
        allowed_additions = frozenset(additive)
        entities = describe_snapshots(snapshots, operation, union=union)
        for entity in entities:
            state.entity(entity, policies, allowed_additions)
        if not required:
            issues.append(
                Issue(
                    "data.consolidation_unverified",
                    "Consolidation requires explicit candidate postconditions",
                    kind="unverified",
                )
            )
        if index.report.verified and snapshots and not issues:
            operation.consume("states", len(snapshots))
            authorized = tuple(_owned(snapshot, operation) for snapshot in snapshots)
            merged = merge_sources(authorized, union=union, budget=operation)
            merged_report = merged.report
            retain_report(merged.report, operation, issues)
            children.append(merged.report)
            if merged.source is not None and merged.report.verified:
                captured = capture_source(merged.source, "candidate:", operation)
                for declaration in required:
                    check_source = _materialize(
                        captured.witness, merged.source, operation
                    )
                    try:
                        checked = declaration.check(check_source)
                    except Exception as error:
                        raise _CallbackFailure(error) from error
                    operation.consume("work")
                    if not isinstance(checked, Report):
                        raise InputError(
                            "Consolidation postconditions must return Report"
                        )
                    retain_report(checked, operation, issues)
                    operation.consume("states")
                    post_reports.append(checked)
                    children.append(checked)
                if all(report.verified for report in post_reports) and post_reports:
                    source = merged.source
    except _CallbackFailure as failure:
        raise failure.original from None
    except LimitExceeded:
        issues.exhausted("Consolidation exceeded its operation limits")
    except InputError:
        try:
            issues.append(
                Issue(
                    "data.consolidation_input_error",
                    "Invalid consolidation participation or policy",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Consolidation diagnostics exceeded their limits")
    report = Report(
        "entity-consolidation",
        state.checked + len(post_reports),
        tuple(issues),
        requested=max(1, state.checked + len(post_reports)),
        children=(*children, *state.children),
    )
    return Consolidation(
        source if report.verified else None,
        report,
        tuple(post_reports),
        index,
        merged_report,
    )
