"""Reuse native OBO attachments and original scoped vocabulary indexes."""

from dataclasses import replace

from rdflib import BNode, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationStatement
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.obo.reading import read_obo_records
from bijux_ontology.vocabulary.obo.types import OboRecords

from .types import CrossReferenceAssertion

XREF = URIRef("http://www.geneontology.org/formats/oboInOwl#hasDbXref")


class UnsupportedScope(InputError):
    """Valid RDF selection exceeds the qualified physical xref scope profile."""


def qualify_view(view: VocabularyView, budget: Budget) -> None:
    """Bound selected source metadata and reject flattened or incomplete scope."""
    scopes = set()
    for scope, _graph in view._reader.scopes:
        bound_text(scope, budget)
        budget.consume("states")
        scopes.add(scope)
    for scope, source in view._vocabulary._source_handles:
        bound_text(scope, budget)
        if scope not in scopes:
            continue
        metadata = SnapshotMetadata.capture(source.prepared, budget)
        if source.graph_name is not None:
            bound_text(source.graph_name, budget)
        if (
            source.union
            or metadata.union
            or metadata.graph_fidelity != "complete"
            or metadata.literal_fidelity not in ("source_terms", "supplied_terms")
        ):
            raise UnsupportedScope(
                "Cross references require complete physical source scopes"
            )


def qualify_edge(edge: Edge, budget: Budget) -> None:
    """Bound every original exact term before retaining or formatting an assertion."""
    for term in (edge.subject, edge.predicate, edge.object):
        bound_node(term, budget)
    bound_text(edge.scope, budget)


def read_references(
    view: VocabularyView,
    owners: tuple[URIRef, ...],
    budget: Budget,
    issues: IssueCollector,
    reports: list[Report],
    result: list[CrossReferenceAssertion],
    obo_records: list[OboRecords],
) -> None:
    """Keep term-level and exact definition/synonym attachments separate."""

    def retain(reference: CrossReferenceAssertion) -> None:
        """Retain the captured assertion before inspecting its annotation tail."""
        budget.consume("states")
        budget.consume("bytes", 64)
        result.append(reference)
        edge = reference.assertion
        for scope, source in view._vocabulary._source_handles:
            budget.consume("work")
            if scope != edge.scope:
                continue
            name = (
                source.graph_name
                if source.graph_name is not None
                else source.prepared.graph_name
            )
            if name == "@default":
                name = None
            elif name is not None and not isinstance(name, BNode):
                bound_text(name, budget)
                name = URIRef(name)
            if not isinstance(edge.subject, URIRef | BNode):
                raise InputError("Xref annotation subjects require RDF resources")
            if not isinstance(edge.object, URIRef | BNode | Literal):
                raise InputError("Xref annotations require supported exact RDF terms")
            annotation = inspect_annotation_graph(
                source.prepared,
                (AnnotationStatement(edge.subject, edge.predicate, edge.object),),
                graph=name,
                annotation_predicates=(XREF,),
                budget=budget,
            )
            retain_report(annotation.report, budget, issues)
            reports.append(annotation.report)
            budget.consume("states")
            budget.consume("bytes", 64)
            result[-1] = replace(reference, annotations=annotation)
            return
        raise InputError("Xref assertion lacks an owned selected source")

    for owner in owners:
        selected = view.relationships(
            str(owner), predicates=(str(XREF),), budget=budget
        )
        retain_report(selected.report, budget, issues)
        reports.append(selected.report)
        for edge in selected.items:
            qualify_edge(edge, budget)
            retain(CrossReferenceAssertion(owner, edge))
        # The native reader owns full annotation interpretation. Its text/type
        # policies are observational here; bounded capture and actual selected
        # xref attachment identity are separate required contracts.
        budget.consume("states", 2)
        budget.consume("bytes", 16)
        native = read_obo_records(view, str(owner), budget=budget)
        obo_records.append(native)
        capture_complete = (
            native.report.checked == native.report.requested
            and all(record.complete for record in native.records)
            and all(attachment.complete for attachment in native.attachments)
        )
        captured = Report(
            "obo.xref_record_capture",
            native.report.checked,
            tuple(
                issue
                for issue in native.report.issues
                if issue.kind in ("input_error", "exhausted")
            ),
            allow_empty=True,
            requested=native.report.requested,
            complete=capture_complete,
            regime="native-obo-record-capture",
        )
        reports.append(captured)
        retain_report(captured, budget, issues)
        # A record may point to several separately qualified attachment nodes.
        # Bound all owned edges before exposing any such composite reference.
        for record in native.records:
            qualify_edge(record.assertion, budget)
        for attachment in native.attachments:
            bound_node(attachment.node, budget)
            bound_text(attachment.scope, budget)
            for edges in (
                attachment.assertions,
                attachment.statements,
                attachment.qualifiers,
            ):
                for edge in edges:
                    qualify_edge(edge, budget)
        for attachment in native.attachments:
            matched = None
            for record in native.records:
                budget.consume("work", len(record.attachments) + 1)
                if any(candidate is attachment for candidate in record.attachments):
                    matched = record
                    break
            for edge in attachment.qualifiers:
                budget.consume("work")
                if edge.predicate == XREF:
                    retain(CrossReferenceAssertion(owner, edge, attachment, matched))
