"""Selected list edits preserve sibling structure and require actual contracts."""

import importlib.util
import threading
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report, combine_reports
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.migrations.lists import (
    ListInsert,
    ListMigrationCandidate,
    ListRemove,
    ListReorder,
    ListTarget,
    migrate_list,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.structure import read_rdf_list
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.skos.collections import check_collection_sequence
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary

E = Namespace("urn:list-migration:")


def _source() -> PreparedSource:
    """Two roots share a tail whose assertions must stay intact."""
    head, tail, last = BNode(), BNode(), BNode()
    graph = Graph()
    for triple in (
        (E.root, SKOS.memberList, head),
        (E.sibling, SKOS.memberList, tail),
        (head, RDF.first, E.a),
        (head, RDF.rest, tail),
        (tail, RDF.first, E.b),
        (tail, RDF.rest, last),
        (last, RDF.first, E.c),
        (last, RDF.rest, RDF.nil),
        (tail, E.annotation, Literal("original", lang="en")),
        (E.root, SKOS.member, E.extra),
    ):
        graph.add(triple)
    return prepare_rdf(graph)


def _target(source: PreparedSource) -> ListTarget:
    """Select the actual immutable head term exported by this snapshot."""
    return ListTarget(
        E.root,
        SKOS.memberList,
        cast(BNode, source.graph().value(E.root, SKOS.memberList)),
    )


def _contract(expected: tuple[URIRef, ...]) -> MigrationCheck:
    """Run existing actual ordered-collection checks for root and sibling."""

    def check(source: PreparedSource) -> Report:
        view = inspect_vocabulary(source.graph())
        return combine_reports(
            "actual-sequences",
            check_collection_sequence(
                view, str(E.root), expected=tuple(map(str, expected))
            ),
            check_collection_sequence(
                view, str(E.sibling), expected=(str(E.b), str(E.c))
            ),
        )

    return MigrationCheck("sequences", check)


def _reorder(
    source: PreparedSource,
    *,
    limits: Limits | None = None,
    cancel: threading.Event | None = None,
) -> ListMigrationCandidate:
    """Change the selected order, retaining failed baseline and actual after truth."""
    return migrate_list(
        source,
        _target(source),
        ListReorder((2, 0, 1)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.c, E.a, E.b),
        before_checks=(_contract((E.c, E.a, E.b)),),
        after_checks=(_contract((E.c, E.a, E.b)),),
        limits=limits,
        cancel=cancel,
    )


def test_shared_tail_reorder_preserves_sibling_and_requires_accepted_export() -> None:
    """Fresh-chain COW changes one root without deleting any original assertion."""
    source = _source()
    original = set(source.graph())
    candidate = _reorder(source)
    assert (
        candidate.source is not None
        and candidate.patch is not None
        and candidate.report.verified
    )
    assert candidate.before[0].outcome == "disagreement" and candidate.after[0].verified
    assert (
        candidate.original_size
        == candidate.candidate_size
        == candidate.copied_cells
        == 3
    )
    assert set(source.graph()) == original
    assert len(candidate.patch.removals) == 1
    assert all(
        quad.predicate not in (RDF.first, RDF.rest) for quad in candidate.patch.removals
    )
    graph = candidate.source.graph()
    sibling_head = graph.value(E.sibling, SKOS.memberList)
    assert sibling_head is not None
    assert read_rdf_list(graph, sibling_head) == (E.b, E.c)
    assert (sibling_head, E.annotation, Literal("original", lang="en")) in graph
    assert (E.root, SKOS.member, E.extra) in graph
    assert export_migration(candidate, format="turtle").content is None
    exported = export_migration(candidate, accept=True, format="turtle")
    assert exported.content is not None and exported.report.verified
    noop = migrate_list(
        source,
        _target(source),
        ListReorder((0, 1, 2)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.a, E.b, E.c),
        before_checks=(_contract((E.a, E.b, E.c)),),
        after_checks=(_contract((E.a, E.b, E.c)),),
    )
    assert (
        noop.report.verified
        and noop.patch is not None
        and not noop.patch.removals
        and not noop.patch.additions
    )
    assert noop.copied_cells == 0


def test_exact_insert_remove_preserves_duplicate_literals_and_empty_list() -> None:
    """Positions distinguish repeated values and retain original numeric spelling."""
    one = literal_term("01", datatype=str(XSD.integer))
    two = literal_term("1", datatype=str(XSD.integer))
    source = prepare_rdf(Graph().add((E.root, E.items, RDF.nil)))
    target = ListTarget(E.root, E.items, RDF.nil)

    def exact(expected: tuple[URIRef | Literal, ...]) -> MigrationCheck:
        def check(value: PreparedSource) -> Report:
            graph = value.graph()
            heads = tuple(graph.objects(E.root, E.items))
            actual = read_rdf_list(graph, heads[0]) if len(heads) == 1 else ()
            from bijux_ontology.diagnostics.reports import Issue

            return Report(
                "exact-positions",
                1,
                ()
                if tuple(map(term_key, actual)) == tuple(map(term_key, expected))
                else (Issue("test.order", "Actual sequence differs"),),
            )

        return MigrationCheck("exact-sequence", check)

    inserted = migrate_list(
        source,
        target,
        ListInsert(0, (one, two, one)),
        expected_before=(),
        expected_after=(one, two, one),
        before_checks=(exact(()),),
        after_checks=(exact((one, two, one)),),
    )
    assert inserted.source is not None and inserted.report.verified
    head = cast(BNode, inserted.source.graph().value(E.root, E.items))
    removed = migrate_list(
        inserted.source,
        ListTarget(E.root, E.items, head),
        ListRemove((0, 2)),
        expected_before=(one, two, one),
        expected_after=(two,),
        before_checks=(exact((one, two, one)),),
        after_checks=(exact((two,)),),
    )
    assert removed.source is not None and removed.report.verified
    head = cast(BNode, removed.source.graph().value(E.root, E.items))
    emptied = migrate_list(
        removed.source,
        ListTarget(E.root, E.items, head),
        ListRemove((0,)),
        expected_before=(two,),
        expected_after=(),
        before_checks=(exact((two,)),),
        after_checks=(exact(()),),
    )
    assert emptied.source is not None and emptied.report.verified
    assert (E.root, E.items, RDF.nil) in emptied.source.graph()


def test_malformed_scope_foreign_blanks_and_internal_anchors_never_publish() -> None:
    """Explicit roots and physical graphs prevent sibling and scope corruption."""
    source = _source()
    head = _target(source).head
    cases = (
        (source, ListTarget(E.root, SKOS.memberList, BNode(str(head)))),
        (
            source,
            ListTarget(
                head, RDF.rest, cast(BNode, source.graph().value(head, RDF.rest))
            ),
        ),
        (source, ListTarget(head, RDF.first, E.a)),
    )
    for original, target in cases:
        result = migrate_list(
            original,
            target,
            ListReorder((2, 0, 1)),
            expected_before=(E.a, E.b, E.c),
            expected_after=(E.c, E.a, E.b),
        )
        assert result.source is None and not result.report.verified
    for malformed in ("cycle", "branch", "missing"):
        graph = source.graph()
        if malformed == "cycle":
            graph.set((head, RDF.rest, head))
        elif malformed == "branch":
            graph.add((head, RDF.first, E.other))
        else:
            graph.remove((head, RDF.rest, None))
        original = prepare_rdf(graph)
        result = _reorder(original)
        assert result.source is None and any(
            i.code == "rdf.list_migration_rejected" for i in result.report.issues
        )
    dataset = Dataset()
    for triple in source.graph():
        dataset.graph(E.g).add(triple)
    dataset.graph(E.hidden).add((E.hidden, E.annotation, Literal("untouched")))
    original = prepare_rdf(dataset)
    owned_head = cast(BNode, original.graph(E.g).value(E.root, SKOS.memberList))
    absent = migrate_list(
        original,
        ListTarget(E.root, SKOS.memberList, owned_head),
        ListRemove((0,)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.b, E.c),
    )
    assert absent.source is None
    selected = migrate_list(
        original,
        ListTarget(E.root, SKOS.memberList, owned_head, E.g),
        ListRemove((0,)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.b, E.c),
    )
    assert selected.source is not None and not selected.report.verified
    assert (
        str(selected.source.graph(E.hidden).value(E.hidden, E.annotation))
        == "untouched"
    )
    pinned = prepare_rdf(RdfSource(dataset, graph_name=str(E.hidden)))
    rejected = migrate_list(
        pinned,
        ListTarget(E.root, SKOS.memberList, owned_head, E.g),
        ListRemove((0,)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.b, E.c),
    )
    assert rejected.source is None and not rejected.report.verified


def test_intent_mismatch_bounds_cancellation_and_missing_contracts_are_atomic() -> None:
    """No positional or required-validation failure can hide behind a patch."""
    source = _source()
    for action, before, after in (
        (ListReorder((0, 1)), (E.a, E.b, E.c), (E.a, E.b)),
        (ListRemove((9,)), (E.a, E.b, E.c), (E.a, E.b)),
        (ListInsert(9, (E.other,)), (E.a, E.b, E.c), (E.other,)),
        (ListReorder((2, 0, 1)), (E.other,), (E.c, E.a, E.b)),
        (ListReorder((2, 0, 1)), (E.a, E.b, E.c), (E.a, E.b, E.c)),
    ):
        result = migrate_list(
            source,
            _target(source),
            action,
            expected_before=before,
            expected_after=after,
        )
        assert result.source is None and result.report.outcome == "disagreement"
    with pytest.raises(InputError):
        ListInsert(True, ())
    with pytest.raises(InputError):
        ListReorder((0, 0))
    cancelled = threading.Event()
    cancelled.set()
    for result in (
        _reorder(source, limits=Limits(max_work=10)),
        _reorder(source, cancel=cancelled),
    ):
        assert result.source is None and result.report.outcome == "exhausted"
    fresh = _reorder(source)
    assert fresh.report.verified
    missing = migrate_list(
        source,
        _target(source),
        ListReorder((2, 0, 1)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.c, E.a, E.b),
    )
    assert missing.source is not None and not missing.report.verified
    assert export_migration(missing, accept=True, format="turtle").content is None
    budget = Budget(Limits(max_issues=1))
    budget.consume("issues")
    exhausted = migrate_list(
        source,
        ListTarget(_target(source).head, RDF.first, E.a),
        ListRemove((0,)),
        expected_before=(E.a,),
        expected_after=(),
        budget=budget,
    )
    assert exhausted.report.outcome == "exhausted" and exhausted.source is None


def test_actual_shacl_order_failure_or_missing_extra_blocks_export() -> None:
    """An optional maintained backend validates actual changed positions."""
    from bijux_ontology.shacl.checking import validate_shapes

    shapes = prepare_rdf(
        """@prefix sh:<http://www.w3.org/ns/shacl#>.
        @prefix skos:<http://www.w3.org/2004/02/skos/core#>.
        @prefix rdf:<http://www.w3.org/1999/02/22-rdf-syntax-ns#>.
        <urn:Shape> a sh:NodeShape; sh:targetNode <urn:list-migration:root>;
          sh:property [sh:path (skos:memberList rdf:rest rdf:first);
            sh:hasValue <urn:list-migration:b>].""",
        format="turtle",
    )
    contract = MigrationCheck(
        "second-position", lambda source: validate_shapes(source, shapes).report
    )
    source = _source()
    result = migrate_list(
        source,
        _target(source),
        ListReorder((2, 0, 1)),
        expected_before=(E.a, E.b, E.c),
        expected_after=(E.c, E.a, E.b),
        before_checks=(contract,),
        after_checks=(contract,),
    )
    assert result.source is not None and not result.report.verified
    assert result.before and result.after and not result.after[0].verified
    if importlib.util.find_spec("pyshacl") is None:
        assert not result.before[0].verified
    else:
        assert result.before[0].verified
        assert result.after[0].outcome == "disagreement"
    assert export_migration(result, accept=True, format="turtle").content is None
