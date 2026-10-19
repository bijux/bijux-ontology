"""Compose existing annotation, OBO subset and supplied lifecycle contracts."""

from collections.abc import Sequence

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Location, Report
from bijux_ontology.vocabulary.annotations.coverage import check_annotation_coverage
from bijux_ontology.vocabulary.annotations.types import (
    AnnotationCoverage,
    AnnotationRequirement,
)
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.browsing.results import Edge, Selection
from bijux_ontology.vocabulary.obo.reading import read_obo_records
from bijux_ontology.vocabulary.obo.subsets.reading import read_obo_subsets
from bijux_ontology.vocabulary.obo.subsets.types import OboSubsets
from bijux_ontology.vocabulary.obo.types import OboRecord, OboRecords
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.upgrades.lifecycle.checking import check_lifecycle
from bijux_ontology.workflows.upgrades.lifecycle.types import (
    HistoryCoverage,
    LifecycleCheck,
    Release,
    Tombstone,
)

from .types import OboPublication, OboPublicationProfile

_MEMBER = "http://www.geneontology.org/formats/oboInOwl#inSubset"


def _text(node: Node) -> bool:
    """The selected native publication rule accepts actual nonempty text only."""
    return (
        isinstance(node, Literal)
        and (node.datatype in (None, XSD.string) or node.language is not None)
        and bool(str(node).strip())
    )


def _record(
    record: OboRecord,
    profile: OboPublicationProfile,
    budget: Budget,
    issues: IssueCollector,
) -> None:
    """Require qualifiers on their actual attachment, never equal lexical text."""
    edge = record.assertion

    def finding(code: str, message: str) -> None:
        issues.append(
            Issue(
                code,
                message,
                str(edge.subject),
                details=Location(source=edge.scope, term=str(edge.subject)),
                actual=str(edge.predicate),
            )
        )

    budget.consume("work")
    if not _text(edge.object):
        finding("obo.publication_text", "Selected OBO text must be nonempty text")
    required = (
        profile.definition_xrefs
        if record.role == "definition"
        else profile.synonym_types is not None
    )
    if not required:
        return
    if not record.attachments:
        finding(
            "obo.publication_qualification",
            "Selected OBO annotation lacks a resolved qualified attachment",
        )
    for attachment in record.attachments:
        budget.consume("work")
        if record.role == "definition":
            references = attachment.cross_references
            budget.consume("work", len(references))
            if not references or any(not _text(e.object) for e in references):
                finding(
                    "obo.publication_definition_xref",
                    "Each selected definition attachment requires nonempty "
                    "text dbxref qualifiers",
                )
        else:
            types = attachment.synonym_types
            budget.consume("work", len(types) + len(profile.synonym_types or ()))
            if (
                len(types) != 1
                or not isinstance(types[0].object, URIRef)
                or str(types[0].object) not in (profile.synonym_types or ())
            ):
                finding(
                    "obo.publication_synonym_type",
                    "Each selected synonym attachment requires exactly one "
                    "allowed named synonym type",
                )


def _subset_edges(
    view: VocabularyView,
    terms: tuple[URIRef, ...],
    support: VocabularyView,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[Edge, ...]:
    """Retain only selected memberships and their referenced definition metadata."""
    result: list[Edge] = []
    referenced: set[URIRef] = set()
    for term in terms:
        selected = view.statements(term, predicates=(_MEMBER,), budget=budget)
        retain_report(selected.report, budget, issues)
        for edge in selected.items:
            budget.consume("states")
            budget.consume("bytes", 8)
            result.append(edge)
            if isinstance(edge.object, URIRef) and edge.object not in referenced:
                budget.consume("states")
                referenced.add(edge.object)
    for iri in sorted(referenced, key=str):
        selected = support.statements(iri, budget=budget)
        retain_report(selected.report, budget, issues)
        for edge in selected.items:
            budget.consume("work")
            if str(edge.predicate) == _MEMBER:
                continue
            budget.consume("states")
            budget.consume("bytes", 8)
            result.append(edge)
    return tuple(result)


def _subset_view(edges: tuple[Edge, ...], budget: Budget) -> VocabularyView:
    """Project exact selected assertions; separate each original scope's blanks."""
    graph = Graph()
    blanks: dict[tuple[str, BNode], BNode] = {}

    def node(value: Node, scope: str) -> Node:
        """Keep blanks shared within their original scope only."""
        bound_node(value, budget)
        if not isinstance(value, BNode):
            return value
        key = scope, value
        if key not in blanks:
            budget.consume("states")
            budget.consume("bytes", 64)
            blanks[key] = BNode()
        return blanks[key]

    for edge in edges:
        bound_text(edge.scope, budget)
        budget.consume("work")
        budget.consume("triples")
        graph.add(
            (
                node(edge.subject, edge.scope),
                edge.predicate,
                node(edge.object, edge.scope),
            )
        )
    return inspect_vocabulary(
        load_vocabulary(graph, budget=budget), scopes=("local",), budget=budget
    )


def check_obo_publication(
    release: Release,
    terms: Sequence[URIRef],
    *,
    profile: OboPublicationProfile,
    history: Sequence[Release] = (),
    coverage: HistoryCoverage | None = None,
    tombstones: Sequence[Tombstone] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OboPublication:
    """Check an explicitly selected finite OBO-in-OWL publication policy.

    The native convention is the OBO Format 1.4 OWL mapping for IAO definitions,
    four oboInOwl synonym predicates, OWL annotation attachments and inSubset.
    Required counts/languages remain ordinary explicit annotation contracts.
    All unselected recommendations remain outside the contract; this does not
    certify the OBO Foundry principles or infer ontology correctness.

    terms declares the owned selection; explicit exclusions may remove imported
    support terms. Each Release owns its offline supplied import bindings.
    Import presence never establishes term ownership.
    Annotation/record obligations use the publication local scope only; imported
    annotations cannot supply a missing owned-term definition or qualifier.
    Only selected memberships and referenced subset metadata enter the native
    subset check. subset_evidence retains their original source scopes; its
    projected subset graph does not represent the whole ontology.
    Lifecycle always ends in this exact release and requires its existing exact
    supplied-interval contract. No unobserved or permanent stability is asserted.
    """
    if not isinstance(release, Release) or not isinstance(
        profile, OboPublicationProfile
    ):
        raise InputError("Expected a Release and explicit OboPublicationProfile")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected an optional shared Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Publication limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    selected: list[URIRef] = []
    excluded: list[URIRef] = []
    records: list[OboRecords] = []
    reports: list[Report] = []
    annotations: Selection[AnnotationCoverage] | None = None
    subsets: OboSubsets | None = None
    subset_evidence: tuple[Edge, ...] = ()
    lifecycle: LifecycleCheck | None = None
    checked = 0
    finished = False
    enabled = bool(
        profile.annotations
        or profile.record_annotations
        or profile.subset_membership
        or profile.lifecycle is not None
    )

    def reserve(count: int = 1) -> None:
        """Reserve output references before an inner operation can pay findings."""
        operation.consume("states", count)
        operation.consume("bytes", 8 * count)

    try:
        SnapshotMetadata.capture(release.source, operation)
        bound_text(release.label, operation)
        capture_items(profile.annotations, AnnotationRequirement, operation)
        for values in (
            profile.exclude_terms,
            profile.exclude_namespaces,
            profile.synonym_types if profile.synonym_types is not None else (),
        ):
            capture_items(values, str, operation)
            for value in values:
                bound_text(value, operation)
        supplied = capture_items(terms, URIRef, operation)
        seen: set[URIRef] = set()
        for term in supplied:
            bound_node(term, operation)
            if term in seen:
                raise InputError("Selected publication terms must be unique")
            reserve(2)
            operation.consume(
                "work", len(profile.exclude_terms) + len(profile.exclude_namespaces)
            )
            seen.add(term)
            if str(term) in profile.exclude_terms or str(term).startswith(
                profile.exclude_namespaces
            ):
                excluded.append(term)
            else:
                selected.append(term)
        if not enabled:
            finished = True
        elif not selected:
            issues.append(
                Issue(
                    "obo.publication_unverified",
                    "Selected publication requirements have no remaining owned terms",
                    kind="unverified",
                )
            )
        else:
            vocabulary = load_vocabulary(
                release.source, imports=release.imports, budget=operation
            )
            view = inspect_vocabulary(vocabulary, scopes=("local",), budget=operation)
            chosen = tuple(selected)
            if profile.annotations:
                reserve()
                annotations = check_annotation_coverage(
                    view,
                    profile.annotations,
                    entities=tuple(str(term) for term in chosen),
                    budget=operation,
                )
                reports.append(annotations.report)
                retain_report(annotations.report, operation, issues)
            for term in chosen:
                operation.consume("work")
                if view.lookup(str(term)).status == "absent":
                    issues.append(
                        Issue(
                            "obo.publication_unverified",
                            "Selected publication term is absent from supplied scope",
                            str(term),
                            kind="unverified",
                        )
                    )
                if profile.record_annotations:
                    reserve(2)
                    result = read_obo_records(view, str(term), budget=operation)
                    records.append(result)
                    reports.append(result.report)
                    retain_report(result.report, operation, issues)
                    for record in result.records:
                        _record(record, profile, operation, issues)
                checked += 1
            if profile.subset_membership:
                support = inspect_vocabulary(vocabulary, budget=operation)
                subset_evidence = _subset_edges(
                    view, chosen, support, operation, issues
                )
                subset_view = _subset_view(subset_evidence, operation)
                reserve()
                subsets = read_obo_subsets(subset_view, closed=True, budget=operation)
                reports.append(subsets.report)
                retain_report(subsets.report, operation, issues)
            if profile.lifecycle is not None:
                previous = capture_items(history, Release, operation)
                reserve()
                lifecycle = check_lifecycle(
                    (*previous, release),
                    chosen,
                    policy=profile.lifecycle,
                    coverage=coverage,
                    tombstones=tombstones,
                    budget=operation,
                )
                reports.append(lifecycle.report)
                retain_report(lifecycle.report, operation, issues)
            finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location=release.label)
    except InputError as error:
        unavailable = isinstance(error, ResourceError)
        try:
            issues.append(
                Issue(
                    "obo.publication_unverified" if unavailable else "input.invalid",
                    str(error),
                    error.location or release.label,
                    kind="unverified" if unavailable else "input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining publication scope failure")
    return OboPublication(
        release,
        tuple(selected),
        tuple(excluded),
        tuple(records),
        annotations,
        subsets,
        subset_evidence,
        lifecycle,
        Report(
            "obo.publication",
            checked,
            tuple(issues),
            allow_empty=not enabled,
            requested=(max(checked, len(selected)) if enabled else 0)
            if finished
            else checked + 1,
            complete=finished,
            children=tuple(reports),
            regime="selected-obo-in-owl-publication",
        ),
    )
