"""Finite lifecycle policy evaluation over existing owned vocabulary evidence."""

from collections.abc import Sequence

from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Location, Report
from bijux_ontology.vocabulary.comparison import VocabularyChanges, compare_vocabularies
from bijux_ontology.vocabulary.facts.comparison import _collect, _metadata
from bijux_ontology.vocabulary.facts.types import FactSelection, SelectedFact
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

from .types import (
    HistoryCoverage,
    LifecycleCheck,
    LifecycleEvent,
    LifecycleObservation,
    LifecyclePolicy,
    Release,
    Tombstone,
)


def _source(release: Release, budget: Budget) -> bool:
    """Qualify original descriptive metadata before retaining its source reference."""
    bound_text(release.label, budget)
    metadata = SnapshotMetadata.capture(release.source, budget)
    return (
        not metadata.union
        and metadata.graph_fidelity == "complete"
        and metadata.literal_fidelity in ("source_terms", "supplied_terms")
    )


def _facts(
    view: VocabularyView,
    selection: FactSelection,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[SelectedFact, ...]:
    """Reuse the selected-fact collector, including original anonymous evidence."""
    inventory = _collect(view, selection, _metadata(selection, budget), budget, issues)
    count = len(inventory.facts) + len(inventory.anonymous_facts)
    budget.consume("states", count)
    budget.consume("bytes", 8 * count)
    return (*inventory.facts.values(), *inventory.anonymous_facts)


def check_lifecycle(
    releases: Sequence[Release],
    terms: Sequence[URIRef],
    *,
    policy: LifecyclePolicy,
    coverage: HistoryCoverage | None = None,
    tombstones: Sequence[Tombstone] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> LifecycleCheck:
    """Check observed retirement and caller policy within an exact supplied interval.

    Absence of a retirement annotation never establishes active status. Explicit
    false after earlier retirement establishes reactivation; an explicitly selected
    disappearance/declaration rule additionally requires complete interval coverage.
    Reserved logical facts are a separate caller policy, including retained facts
    after retirement. Editorial differences alone never establish identifier reuse.

    The original snapshots and adjacent comparisons remain evidence. No inference,
    unobserved release, semantic-intention claim or permanent stability follows.
    """
    if not isinstance(policy, LifecyclePolicy):
        raise InputError("Expected an explicit LifecyclePolicy")
    if coverage is not None and not isinstance(coverage, HistoryCoverage):
        raise InputError("Expected an explicit HistoryCoverage")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    observations: list[LifecycleObservation] = []
    events: list[LifecycleEvent] = []
    changes: list[VocabularyChanges] = []
    reports: list[Report] = []
    adopted_coverage = None
    completed = 0
    requested = 1
    finished = False

    def unknown(message: str, term: URIRef | None = None) -> None:
        """Keep required history limitations separate from observed policy findings."""
        issues.append(
            Issue(
                "upgrade.lifecycle_unverified",
                message,
                str(term) if term is not None else "",
                kind="unverified",
            )
        )

    def event(value: LifecycleEvent) -> None:
        """Reserve event storage before preserving a completed observable finding."""
        operation.consume("states")
        operation.consume("bytes", 96)
        events.append(value)
        if value.prohibited:
            issues.append(
                Issue(
                    "upgrade.lifecycle_violation",
                    "Observed lifecycle event is prohibited by the supplied policy",
                    str(value.term),
                    actual=value.kind,
                )
            )

    def adopt(report: Report, *, observational: bool = False) -> None:
        """Bound the raw child before attaching its required report reference."""
        operation.consume("states")
        operation.consume("bytes", 8)
        if observational:
            retained = IssueCollector(operation)
            retain_report(report, operation, retained)
            issues.extend(issue for issue in retained if issue.kind != "finding")
        else:
            retain_report(report, operation, issues)
            reports.append(report)

    try:
        supplied = capture_items(releases, Release, operation)
        selected = capture_items(terms, URIRef, operation)
        markers = capture_items(tombstones, Tombstone, operation)
        labels: dict[str, int] = {}
        for index, release in enumerate(supplied):
            bound_text(release.label, operation)
            if release.label in labels:
                raise InputError("Release labels must be unique and caller ordered")
            operation.consume("states")
            labels[release.label] = index
        selected_set: set[URIRef] = set()
        for term in selected:
            bound_node(term, operation)
            if term in selected_set:
                raise InputError("Selected lifecycle terms must be unique")
            operation.consume("states")
            selected_set.add(term)
        for rule in policy.reserved:
            bound_text(rule.iri, operation)
            for role in (rule.subject_role, rule.object_role):
                if role is not None:
                    bound_text(role, operation)
        for position in policy.positions:
            bound_text(position, operation)
        bound_text(policy.reactivation, operation)
        if len({rule.iri for rule in policy.reserved}) != len(policy.reserved):
            raise InputError("Reserved predicate rules must be unique")
        by_marker: dict[tuple[str, URIRef], Tombstone] = {}
        for declared_marker in markers:
            bound_node(declared_marker.term, operation)
            bound_text(declared_marker.release, operation)
            key = (declared_marker.release, declared_marker.term)
            if (
                declared_marker.release not in labels
                or declared_marker.term not in selected_set
                or key in by_marker
            ):
                raise InputError(
                    "Tombstones must uniquely match selected terms/releases"
                )
            operation.consume("states")
            by_marker[key] = declared_marker
        complete_interval = False
        if coverage is not None:
            operation.consume("states", len(coverage.labels))
            for label in coverage.labels:
                bound_text(label, operation)
            complete_interval = coverage.complete and coverage.labels == tuple(labels)
            adopted_coverage = coverage
        if not complete_interval:
            unknown("History does not certify the exact supplied release interval")
        if len(supplied) < 2 or not selected:
            unknown("Lifecycle checks require selected terms and at least two releases")
        requested = max(1, len(supplied) * len(selected))
        selection = (
            FactSelection(
                policy.reserved,
                entities=tuple(str(term) for term in selected),
                positions=policy.positions,
            )
            if policy.reserved
            else None
        )
        retired: dict[URIRef, str] = {}
        previous: dict[URIRef, LifecycleObservation] = {}
        previous_view = None
        for release in supplied:
            if not _source(release, operation):
                unknown("Exact literal terms and physical selected graphs are required")
                break
            vocabulary = load_vocabulary(
                release.source, imports=release.imports, budget=operation
            )
            view = inspect_vocabulary(vocabulary, scopes=("local",), budget=operation)
            facts = _facts(view, selection, operation, issues) if selection else ()
            by_term: dict[URIRef, list[SelectedFact]] = {}
            for fact in facts:
                matched: set[URIRef] = set()
                for occurrence in fact.occurrences:
                    operation.consume("work")
                    if occurrence.position not in policy.positions or not isinstance(
                        occurrence.term, URIRef
                    ):
                        continue
                    bound_node(occurrence.term, operation)
                    if (
                        occurrence.term not in selected_set
                        or occurrence.term in matched
                    ):
                        continue
                    operation.consume("states", 2)
                    operation.consume("bytes", 16)
                    matched.add(occurrence.term)
                    by_term.setdefault(occurrence.term, []).append(fact)
                    if occurrence.role == "unknown":
                        unknown(
                            "Reserved occurrence has an unknown logical role",
                            occurrence.term,
                        )
            if previous_view is not None:
                operation.consume("states")
                operation.consume("bytes", 8)
                change = compare_vocabularies(
                    previous_view, view, fact_selection=selection, budget=operation
                )
                adopt(change.report, observational=True)
                changes.append(change)
                if change.report.outcome == "exhausted":
                    raise LimitExceeded(
                        "Adjacent supplied release comparison exhausted"
                    )
            for term in selected:
                operation.consume("work")
                state = view.deprecation(str(term), budget=operation)
                adopt(state.report)
                declaration = view.lookup(str(term))
                selected_facts = by_term.get(term, [])
                marker = by_marker.get((release.label, term))
                operation.consume("states")
                operation.consume("bytes", 192 + 8 * len(selected_facts))
                observation = LifecycleObservation(
                    release, term, declaration, state, marker, tuple(selected_facts)
                )
                observations.append(observation)
                old = previous.get(term)
                retirement = retired.get(term)
                if state.state in ("conflicting", "unverified"):
                    unknown("Retirement annotation is conflicting or unsupported", term)
                if marker is not None and state.state == "active":
                    unknown("Explicit tombstone conflicts with active annotation", term)
                reactivated = state.state == "active" and (
                    old is None or old.deprecation.state != "active"
                )
                appeared = (
                    policy.declaration_reactivation
                    and old is not None
                    and old.declaration.status == "absent"
                    and declaration.status == "declared"
                )
                if retirement is not None and retirement != release.label:
                    if appeared and not complete_interval:
                        unknown(
                            "Declaration reappearance lacks complete history coverage",
                            term,
                        )
                    if reactivated or appeared and complete_interval:
                        event(
                            LifecycleEvent(
                                "reactivated",
                                term,
                                release.label,
                                retirement,
                                policy.reactivation == "forbid",
                            )
                        )
                    for fact in selected_facts:
                        if any(
                            isinstance(node, BNode)
                            for node in (*fact.statement, fact.graph.name)
                        ):
                            unknown(
                                "Anonymous reserved facts retain uncertain "
                                "cross-release attribution",
                                term,
                            )
                        event(
                            LifecycleEvent(
                                "reserved_assertion",
                                term,
                                release.label,
                                retirement,
                                policy.forbid_retired_assertions,
                                fact,
                            )
                        )
                if (
                    marker is not None or state.state == "deprecated"
                ) and retirement is None:
                    operation.consume("states")
                    retired[term] = release.label
                    event(
                        LifecycleEvent(
                            "retired", term, release.label, release.label, False
                        )
                    )
                previous[term] = observation
                completed += 1
            previous_view = view
        operation.consume("work", 0)
        finished = completed == requested and bool(selected) and len(supplied) >= 2
    except LimitExceeded as error:
        issues.exhausted(str(error), location="upgrade.lifecycle")
    except ResourceError as error:
        try:
            issues.append(
                Issue(
                    "upgrade.lifecycle_unverified",
                    "A required release resource is unavailable or unqualified",
                    error.location,
                    kind="unverified",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining unavailable release resource")
    except InputError:
        try:
            issues.append(
                Issue(
                    "upgrade.lifecycle_input_error",
                    "Supplied release, term, tombstone or scope contract is invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining lifecycle input failure")
    return LifecycleCheck(
        tuple(observations),
        tuple(events),
        tuple(changes),
        adopted_coverage,
        Report(
            "upgrade.lifecycle",
            completed,
            tuple(issues),
            requested=requested,
            complete=finished,
            children=tuple(reports),
            regime="explicit-selected-supplied-release-interval",
        ),
    )
