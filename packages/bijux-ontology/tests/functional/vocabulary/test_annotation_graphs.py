"""Recursive annotation witnesses retain exact roots and checked leaf ownership."""

import threading
from dataclasses import replace
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.owl.checking import check_axiom_annotations
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.annotations.graphs import (
    AnnotationStatement,
    AnnotationValueEdit,
    adapt_obo_record,
    compare_annotation_graphs,
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.authoring import author_vocabulary
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo import read_obo_records
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

E = Namespace("urn:annotations:")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
LABEL = Literal("Cell", lang="en")
ROOT = AnnotationStatement(E.Cell, RDFS.label, LABEL)


def _attach(
    graph: Graph, node: URIRef | BNode, root: AnnotationStatement, kind: URIRef
) -> None:
    """Produce the ordinary OWL mapping without inventing replacement identities."""
    for predicate, value in (
        (RDF.type, kind),
        (OWL.annotatedSource, root.subject),
        (OWL.annotatedProperty, root.predicate),
        (OWL.annotatedTarget, root.value),
    ):
        graph.add((node, predicate, value))


def _source(*, shared: bool = False) -> PreparedSource:
    """A resource-valued source qualifier itself has a reviewer annotation."""
    graph = Graph().add((E.Cell, RDFS.label, LABEL))
    graph.add((E.source, RDF.type, OWL.AnnotationProperty))
    graph.add((E.reviewer, RDF.type, OWL.AnnotationProperty))
    graph.add((E.note, RDF.type, OWL.AnnotationProperty))
    graph.add((E.paper, E.note, Literal("Opaque resource body")))
    _attach(graph, BNode("label-attachment"), ROOT, OWL.Axiom)
    graph.add((BNode("label-attachment"), E.source, E.paper))
    graph.add((BNode("label-attachment"), E.note, Literal("Keep", lang="fr")))
    qualified = AnnotationStatement(BNode("label-attachment"), E.source, E.paper)
    _attach(graph, BNode("source-attachment"), qualified, OWL.Annotation)
    graph.add((BNode("source-attachment"), E.reviewer, E.alice))
    if shared:
        graph.add((E.Other, E.reference, BNode("source-attachment")))
    return prepare_rdf(graph)


def _encoding(source: PreparedSource) -> Report:
    """Exercise the maintained existing annotation mapping checker as an after-check."""
    budget = Budget()
    issues = IssueCollector(budget)
    checked = check_axiom_annotations(source.graph(), "", budget, issues)
    return Report("annotation-encoding", checked, tuple(issues))


def _edit(source: PreparedSource) -> AnnotationValueEdit:
    """Select the exact owned root-to-reviewer path from the actual graph."""
    result = inspect_annotation_graph(source, (ROOT,))
    assert result.report.verified
    path = next(path for path in result.paths if path.statement.predicate == E.reviewer)
    return AnnotationValueEdit(ROOT, path.attachments, path.statement, E.bob)


def test_resource_qualifier_reviewer_edit_and_root_attribution() -> None:
    """One exact leaf changes while original label, language and qualifiers survive."""
    source = _source()
    read = inspect_annotation_graph(source, (ROOT,))
    assert read.report.verified and len(read.attachments) == 2
    assert any(
        path.statement.value == E.paper and len(path.attachments) == 1
        for path in read.paths
    )
    assert not any(statement.subject == E.paper for statement in read.statements)
    before = set(source.graph())
    edited = author_vocabulary(
        source, (_edit(source),), after_checks=(MigrationCheck("encoding", _encoding),)
    )
    assert (
        edited.report.verified
        and edited.source is not None
        and edited.patch is not None
    )
    assert len(edited.patch.additions) == len(edited.patch.removals) == 1
    assert set(source.graph()) == before
    actual = edited.source.graph()
    assert (E.Cell, RDFS.label, LABEL) in actual
    assert any(
        value == Literal("Keep", lang="fr") for value in actual.objects(None, E.note)
    )
    assert str(actual.value(E.paper, E.note)) == "Opaque resource body"
    assert list(actual.objects(None, E.reviewer)) == [E.bob]
    comparison = compare_annotation_graphs(
        read, inspect_annotation_graph(edited.source, (ROOT,))
    )
    assert comparison.report.outcome == "disagreement"
    assert comparison.changed_roots == (ROOT,) and not comparison.uncertain_roots
    assert export_migration(edited, accept=True, format="turtle").content is not None
    missing = author_vocabulary(source, (_edit(source),))
    assert not missing.report.verified
    assert export_migration(missing, accept=True, format="turtle").content is None
    rejected = author_vocabulary(
        source,
        (_edit(source),),
        after_checks=(
            MigrationCheck(
                "reviewer",
                lambda _: Report(
                    "reviewer", 1, (Issue("test.reviewer", "Rejected reviewer"),)
                ),
            ),
        ),
    )
    assert rejected.source is not None and not rejected.report.verified
    assert export_migration(rejected, accept=True, format="turtle").content is None


def test_equal_synonyms_and_shared_leaf_are_not_destructively_merged() -> None:
    """Real OBO records adapt through actual source-owned nodes, not lexical form."""
    graph = Graph().add((E.Cell, OIO.hasExactSynonym, Literal("Same", lang="en")))
    for name, reviewer in (("first", E.alice), ("second", E.bob)):
        node = BNode(name)
        root = AnnotationStatement(
            E.Cell, OIO.hasExactSynonym, Literal("Same", lang="en")
        )
        _attach(graph, node, root, OWL.Axiom)
        graph.add((node, E.reviewer, reviewer))
    source = prepare_rdf(graph)
    root = AnnotationStatement(E.Cell, OIO.hasExactSynonym, Literal("Same", lang="en"))
    selected = inspect_annotation_graph(
        source, (root,), annotation_predicates=(E.reviewer,)
    )
    assert selected.report.verified and len(selected.attachments) == 2
    vocabulary = load_vocabulary(source)
    records = read_obo_records(inspect_vocabulary(vocabulary), str(E.Cell))
    assert records.report.verified and len(records.records[0].attachments) == 2
    adapted = adapt_obo_record(selected, records.records[0])
    assert adapted.report.verified and len(adapted.attachments) == 2
    original = records.records[0]
    for attachments in ((), original.attachments[:1], (original.attachments[0],) * 2):
        assert not adapt_obo_record(
            selected, replace(original, attachments=attachments)
        ).report.verified
    copied = read_obo_records(inspect_vocabulary(source.graph()), str(E.Cell))
    assert not adapt_obo_record(selected, copied.records[0]).report.verified
    shared = _source(shared=True)
    shared_read = inspect_annotation_graph(shared, (ROOT,))
    assert any(
        reference.subject == E.Other
        for attachment in shared_read.attachments
        for reference in attachment.references
    )
    failed = author_vocabulary(
        shared, (_edit(shared),), after_checks=(MigrationCheck("encoding", _encoding),)
    )
    assert not failed.report.verified and failed.source is None
    assert list(shared.graph().objects(None, E.reviewer)) == [E.alice]


def test_cycles_conflicting_anchors_and_foreign_blank_paths_remain_unknown() -> None:
    """Malformed/cyclic cases retain original edges and cannot approve a leaf edit."""
    graph = Graph()
    a, b = BNode("a"), BNode("b")
    graph.add((a, E.note, E.value)).add((b, E.note, E.value))
    _attach(graph, b, AnnotationStatement(a, E.note, E.value), OWL.Annotation)
    _attach(graph, a, AnnotationStatement(b, E.note, E.value), OWL.Annotation)
    source = prepare_rdf(graph)
    owned = next(subject for subject in source._read().subjects(E.note, E.value))
    result = inspect_annotation_graph(
        source,
        (AnnotationStatement(cast(BNode, owned), E.note, E.value),),
        annotation_predicates=(E.note,),
    )
    assert not result.report.complete and result.paths and result.attachments
    conflicting = _source()
    raw = conflicting.graph()
    node = next(raw.subjects(E.reviewer, E.alice))
    raw.add((node, OWL.annotatedTarget, E.other))
    malformed = inspect_annotation_graph(prepare_rdf(raw), (ROOT,))
    assert not malformed.report.verified and any(
        issue.code == "annotations.attachment_shape" for issue in malformed.report
    )
    fresh = _source()
    edit = _edit(fresh)
    foreign = replace(
        edit,
        attachments=tuple(
            BNode(str(item)) if isinstance(item, BNode) else item
            for item in edit.attachments
        ),
    )
    rejected = author_vocabulary(
        fresh, (foreign,), after_checks=(MigrationCheck("encoding", _encoding),)
    )
    assert not rejected.report.verified and rejected.source is None


def test_physical_graph_identity_exact_literals_and_bounded_prefixes() -> None:
    """Graph scope, exact lexical forms and bounded tails stay explicit."""
    dataset = Dataset()
    first, other = dataset.graph(E.g), dataset.graph(E.h)
    for graph, reviewer in ((first, E.alice), (other, E.bob)):
        graph.add((E.Cell, RDFS.label, LABEL))
        _attach(graph, BNode(), ROOT, OWL.Axiom)
        node = next(graph.subjects(OWL.annotatedSource, E.Cell))
        graph.add((node, E.reviewer, reviewer))
        graph.add((E.reviewer, RDF.type, OWL.AnnotationProperty))
    source = prepare_rdf(dataset)
    with pytest.raises(InputError):
        inspect_annotation_graph(source, (ROOT,))
    large_graph = inspect_annotation_graph(
        source,
        (ROOT,),
        graph=URIRef("urn:" + "x" * 100000),
        limits=Limits(max_bytes=1000),
    )
    assert large_graph.report.outcome == "exhausted" and large_graph.graph is None
    selected = inspect_annotation_graph(source, (ROOT,), graph=E.g)
    assert selected.report.verified and any(
        path.statement.value == E.alice for path in selected.paths
    )
    assert not any(path.statement.value == E.bob for path in selected.paths)
    changed_scope = compare_annotation_graphs(
        selected, inspect_annotation_graph(source, (ROOT,), graph=E.h)
    )
    assert changed_scope.report.outcome == "disagreement"
    assert changed_scope.changed_roots == (ROOT,)
    numeric = AnnotationStatement(
        E.Cell, E.number, literal_term("01", datatype=str(XSD.integer))
    )
    exact = prepare_rdf(
        Graph().add((numeric.subject, numeric.predicate, numeric.value))
    )
    assert inspect_annotation_graph(exact, (numeric,)).report.verified
    wrong = replace(numeric, value=literal_term("1", datatype=str(XSD.integer)))
    assert not inspect_annotation_graph(exact, (wrong,)).report.verified
    partial = inspect_annotation_graph(_source(), (ROOT,), limits=Limits(max_work=2000))
    assert not partial.report.verified
    malformed = Graph()
    bad_roots = []
    for subject in (E.Bad, E.OtherBad):
        bad = AnnotationStatement(subject, RDFS.label, Literal("Bad"))
        bad_roots.append(bad)
        malformed.add((subject, RDFS.label, bad.value))
        node = BNode()
        _attach(malformed, node, bad, OWL.Axiom)
        malformed.remove((node, OWL.annotatedProperty, None))
    retained = inspect_annotation_graph(
        prepare_rdf(malformed), tuple(bad_roots), limits=Limits(max_issues=1)
    )
    assert retained.report.outcome == "exhausted"
    assert any(
        issue.code == "annotations.attachment_shape" for issue in retained.report
    )
    cancelled = threading.Event()
    cancelled.set()
    assert (
        inspect_annotation_graph(_source(), (ROOT,), cancel=cancelled).report.outcome
        == "exhausted"
    )
    huge = AnnotationStatement(URIRef("urn:" + "x" * 10000), RDFS.label, LABEL)
    result = inspect_annotation_graph(_source(), (huge,), limits=Limits(max_bytes=1000))
    assert result.report.outcome == "exhausted" and all(
        not issue.location for issue in result.report
    )


def test_anonymous_roots_require_stored_subject_and_object_witnesses() -> None:
    graph = Graph().add((BNode("root"), RDFS.label, LABEL))
    graph.add((E.Cell, RDFS.seeAlso, BNode("value")))
    source = prepare_rdf(graph)
    owned = source.graph()
    subject = next(owned.subjects(RDFS.label, None))
    value = next(owned.objects(E.Cell, RDFS.seeAlso))
    label = owned.value(subject, RDFS.label)
    assert isinstance(subject, BNode) and isinstance(value, BNode)
    assert isinstance(label, Literal)
    root = AnnotationStatement(subject, RDFS.label, label)
    link = AnnotationStatement(E.Cell, RDFS.seeAlso, value)
    assert inspect_annotation_graph(source, (root, link)).report.verified
    foreign_subject, foreign_value = BNode(str(subject)), BNode(str(value))
    assert foreign_subject is not subject and foreign_value is not value
    for forged in (
        replace(root, subject=foreign_subject),
        replace(link, value=foreign_value),
    ):
        result = inspect_annotation_graph(source, (forged,))
        assert result.report.outcome == "unverified"
        assert result.report.checked == 0 and not result.paths
    assert (
        inspect_annotation_graph(
            source, (root,), limits=Limits(max_work=1)
        ).report.outcome
        == "exhausted"
    )
