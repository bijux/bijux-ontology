"""Asserted slim classification reuses actual typed relation-path queries."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from rdflib import BNode, URIRef, Variable
from rdflib import Literal as RdfLiteral
from rdflib.namespace import RDFS

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.reports import Issue, Location, Report
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.relation_paths.automaton import named
from bijux_ontology.vocabulary.relation_paths.checking import find_relation_path
from bijux_ontology.vocabulary.relation_paths.types import (
    RelationEndpoint,
    RelationPathResult,
    RelationRepeat,
    RelationSpec,
    RelationStep,
)
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection
from bijux_ontology.vocabulary.results import Selection

from .types import (
    SlimAnnotation,
    SlimAssignment,
    SlimAssignmentSet,
    SlimCount,
    SlimMatch,
    SlimTermAssignment,
)

_DEFAULT_PATH = RelationRepeat(RelationStep(str(RDFS.subClassOf), "class"))


@dataclass
class _TermProgress:
    """Prepaid result slots preserve returned native evidence on terminal limits."""

    term: URIRef
    comparisons: list[RelationPathResult | None] = field(default_factory=list)
    complete: bool = False


def _record(
    annotation: SlimAnnotation, budget: Budget
) -> tuple[SlimAnnotation, URIRef, str]:
    """Qualify original fields before hashing or retaining their owned copy."""
    if not isinstance(annotation, SlimAnnotation):
        raise InputError("Slim annotations require typed record/term pairs")
    record = annotation.record_id
    if not isinstance(record, str) or isinstance(record, (BNode, RdfLiteral, Variable)):
        raise InputError("Record IDs require exact text")
    bound_text(record, budget)
    if not record.strip():
        raise InputError("Record IDs must be nonempty")
    term = named(annotation.term, budget)
    budget.consume("work", 2 * len(record) + 16)
    budget.consume("bytes", 4 * len(record))
    budget.consume("states", 12)
    budget.consume("bytes", 768)
    record_key = str.__str__(record)
    hash(record_key)
    return SlimAnnotation(record, annotation.term), term, record_key


def _finish(
    roots: list[URIRef],
    policy: Literal["nearest", "all"] | None,
    records: list[tuple[SlimAnnotation, URIRef, str]],
    terms: dict[URIRef, _TermProgress],
) -> tuple[tuple[SlimAssignment, ...], tuple[SlimCount, ...], int, int, int, int]:
    """Materialize only prepaid data references and exact aggregate counters.

    Query completion flags are established before this step. No semantic work or
    report adoption occurs after exhaustion. The worst-case record/root product
    was reserved before any term could acquire selected matches.
    """
    classifications = {}
    for term, progress in terms.items():
        comparisons = tuple(
            result for result in progress.comparisons if result is not None
        )
        matches = tuple(
            SlimMatch(root, len(result.steps), result)
            for root, result in zip(roots, progress.comparisons, strict=False)
            if result is not None and result.status == "found"
        )
        selected: tuple[SlimMatch, ...] = ()
        status: Literal["covered", "uncovered", "unknown"] = "unknown"
        if progress.complete:
            status = "covered" if matches else "uncovered"
            if policy == "all":
                selected = matches
            elif matches:
                minimum = min(item.distance for item in matches)
                selected = tuple(item for item in matches if item.distance == minimum)
        classifications[term] = SlimTermAssignment(
            term, status, matches, selected, comparisons
        )
    assignments = []
    occurrences = dict.fromkeys(roots, 0)
    record_sets: dict[URIRef, set[str]] = {root: set() for root in roots}
    uncovered = unknown = multiple = checked = 0
    for index, (original, term, record_key) in enumerate(records):
        classification = classifications[term]
        assignments.append(SlimAssignment(index, original, classification))
        uncovered += classification.status == "uncovered"
        unknown += classification.status == "unknown"
        checked += classification.status != "unknown"
        multiple += len(classification.applicable) > 1
        for match in classification.selected:
            occurrences[match.root] += 1
            record_sets[match.root].add(record_key)
    counts = tuple(
        SlimCount(root, occurrences[root], len(record_sets[root])) for root in roots
    )
    return tuple(assignments), counts, uncovered, unknown, multiple, checked


def assign_slim(
    view: VocabularyView,
    annotations: Sequence[SlimAnnotation],
    *,
    roots: Sequence[str],
    policy: Literal["nearest", "all"],
    path: RelationSpec = _DEFAULT_PATH,
    projections: Selection[RestrictionProjection] | None = None,
    relation_view: str = "asserted",
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> SlimAssignmentSet:
    """Classify original fine terms against explicit roots in a selected view.

    Asserted named subclass navigation is the default; explicit relation paths
    may select supported existential projections. No inference backend is used.
    Nearest means fewest typed-view steps and retains every minimum-distance tie.
    All means every established selected root. Every required root comparison
    must complete before either selection is final. Unknown coverage never means
    uncovered, and neither operation rewrites fine terms or extracts an ontology.

    Repeated occurrences stay distinct. Per-category counts separately count
    annotation occurrences and unique exact case-sensitive record IDs. Native
    comparisons are shared only for the same exact fine IRI. An empty corpus with
    nonempty qualified roots is complete/not_applicable with zero counts, without
    inventing annotation coverage. Original sources and caller inputs stay intact.
    """
    if (
        not isinstance(view, VocabularyView)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected VocabularyView and optional shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    selected_roots: list[URIRef] = []
    records: list[tuple[SlimAnnotation, URIRef, str]] = []
    terms: dict[URIRef, _TermProgress] = {}
    children: list[Report] = []
    selected_policy = None
    requested = 0
    corpus_known = False
    root_complete = False
    finished = False
    try:
        bound_text(policy, operation)
        if policy not in ("nearest", "all"):
            raise InputError("Select nearest or all slim policy")
        selected_policy = policy
        raw_roots = capture(roots, operation)
        if not raw_roots:
            raise InputError("Select a nonempty slim root inventory")
        operation.consume("work", 8 * len(raw_roots))
        operation.consume("states", 8 * len(raw_roots))
        operation.consume("bytes", 512 * len(raw_roots))
        seen = set()
        root_complete = True
        for root in raw_roots:
            iri = named(root, operation)
            if iri in seen:
                continue
            seen.add(iri)
            selected_roots.append(iri)
            if view.lookup(str(iri)).status == "absent":
                root_complete = False
                issues.append(
                    Issue(
                        "terms.slim_root_unverified",
                        "A required slim category is absent from the selected view",
                        details=Location(term=str(iri)),
                        kind="unverified",
                    )
                )
        original = capture(annotations, operation)
        requested = len(original)
        corpus_known = True
        for value in original:
            record = _record(value, operation)
            records.append(record)
            term = record[1]
            if term not in terms:
                terms[term] = _TermProgress(term)
        # This is a bounded per-operation reservation for final row/root counts,
        # not a global worker allowance. Unknown rows never acquire selections.
        product = len(records) * len(selected_roots)
        operation.consume("work", 8 * (product + len(records) + len(selected_roots)))
        operation.consume("states", 4 * product + 3 * len(records))
        operation.consume("bytes", 256 * product + 192 * len(records))
        bound_text(relation_view, operation)
        if relation_view != "asserted":
            issues.append(
                Issue(
                    "terms.slim_view_unverified",
                    "Slim assignment supports the explicitly asserted relation view",
                    kind="unverified",
                )
            )
        else:
            for progress in terms.values():
                count = len(selected_roots)
                operation.consume("work", 4 * count + 1)
                operation.consume("states", 4 * count + 2)
                operation.consume("bytes", 256 + 192 * count)
                progress.comparisons = [None] * count
                for index, root in enumerate(selected_roots):
                    result = find_relation_path(
                        view,
                        progress.term,
                        path,
                        endpoint=RelationEndpoint((root,)),
                        projections=projections,
                        max_depth=max_depth,
                        budget=operation,
                    )
                    progress.comparisons[index] = result
                    children.append(result.report)
                    retain_report(result.report, operation, issues)
                progress.complete = root_complete and all(
                    result is not None
                    and result.report.verified
                    and result.status != "incomplete"
                    for result in progress.comparisons
                )
                if progress.complete and not any(
                    result is not None and result.status == "found"
                    for result in progress.comparisons
                ):
                    issues.append(
                        Issue(
                            "terms.slim_uncovered",
                            "This original fine term has no selected slim category",
                            details=Location(term=str(progress.term)),
                        )
                    )
            finished = True
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Slim assignment exceeded its operation allowance")
    except InputError:
        try:
            issues.append(
                Issue(
                    "terms.slim_invalid",
                    "Slim assignment requires typed annotations and finite named roots",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Slim diagnostic allowance exhausted")
    assignments, counts, uncovered, unknown, multiple, checked = _finish(
        selected_roots, selected_policy, records, terms
    )
    report = Report(
        "terms.slim_assignment",
        checked,
        tuple(issues),
        requested=requested,
        complete=finished
        and root_complete
        and len(records) == requested
        and unknown == 0,
        allow_empty=corpus_known and requested == 0,
        children=tuple(children),
        regime="asserted-slim-assignment",
    )
    return SlimAssignmentSet(
        tuple(selected_roots),
        selected_policy,
        assignments,
        counts,
        uncovered,
        unknown,
        multiple,
        report,
    )
