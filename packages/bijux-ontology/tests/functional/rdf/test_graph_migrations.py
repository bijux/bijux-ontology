"""Actual dataset routing keeps identity, explicit inventory and contract truth."""

import threading
from dataclasses import replace
from typing import cast

from rdflib import BNode, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.query_rules.checking import run_query_rules
from bijux_ontology.data.query_rules.types import QueryRule
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.migrations.graphs import GraphRoute, migrate_graphs
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key

E = Namespace("https://graphs.example/")
_NATIVE_TIMEOUT = 60.0


def test_export_requires_the_scope_origin_and_fidelity_that_passed_after_checks() -> (
    None
):
    source = prepare_rdf(RdfSource(_source().dataset(), graph_name=str(E.G)))
    expected = (True, None, None, "supplied_terms", "complete", str(E.G), False)
    fields = (
        "is_dataset",
        "document_iri",
        "effective_url",
        "literal_fidelity",
        "graph_fidelity",
        "graph_name",
        "union",
    )

    def selected(candidate: PreparedSource) -> Report:
        assert tuple(getattr(candidate, name) for name in fields) == expected
        assert (
            E.keep,
            E.note,
            Literal("unchanged", datatype=XSD.string),
        ) in candidate.graph()
        return Report("selected-scope", 2)

    result = migrate_graphs(
        source,
        (GraphRoute(E.G, E.G),),
        after_checks=(MigrationCheck("selected-scope", selected),),
    )
    assert result.report.verified and result.source is not None
    assert export_migration(result, accept=True, format="trig").report.verified
    changes = (
        False,
        "urn:other-document",
        "https://other.example/",
        "normalized",
        "nonempty-only",
        None,
        True,
    )
    for name, original, changed in zip(fields, expected, changes, strict=True):
        setattr(result.source, name, changed)
        refused = export_migration(result, accept=True, format="trig")
        assert refused.content is None and refused.report.outcome == "unverified", name
        setattr(result.source, name, original)
    restored = export_migration(result, accept=True, format="trig")
    assert restored.report.verified and restored.content is not None
    foreign = prepare_rdf(RdfSource(_source().dataset(), graph_name=str(E.G)))
    swapped = export_migration(
        replace(result, source=foreign), accept=True, format="trig"
    )
    assert swapped.content is None and swapped.report.outcome == "unverified"
    assert source.graph_name == str(E.G) and source.graph_fidelity == "complete"


def _source() -> PreparedSource:
    """Keep one original blank in object, subject and graph-name roles."""
    dataset = Dataset()
    shared = BNode()
    dataset.graph(E.G).add((E.root, E.child, shared))
    dataset.graph(E.G).add((E.keep, E.note, Literal("unchanged")))
    dataset.graph(E.H).add((E.other, E.child, shared))
    dataset.graph(shared).add(
        (shared, E.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    dataset.graph(E.unrelated).add((E.record, E.note, Literal("unrelated")))
    dataset.graph(E.old_empty)
    return prepare_rdf(dataset)


def _check(graph: URIRef, subject: URIRef, predicate: URIRef) -> MigrationCheck:
    """Use actual maintained query evaluation for a required graph-local fact."""
    rule = QueryRule(
        "present",
        f"ASK {{ <{subject}> <{predicate}> ?value }}",
        QueryScope("named", graph),
        expected=True,
    )
    return MigrationCheck(
        "graph-fact", lambda source: run_query_rules(source, (rule,)).report
    )


def test_intentional_merge_preserves_shared_roles_and_explicit_empty_inventory() -> (
    None
):
    source = _source()
    original = frozenset(source.dataset().quads())
    result = migrate_graphs(
        source,
        (GraphRoute(E.G, E.merged), GraphRoute(E.H, E.merged)),
        merge=True,
        added_graphs=(E.merged, E.new_empty),
        removed_graphs=(E.G, E.H, E.old_empty),
        before_checks=(_check(E.G, E.root, E.child),),
        after_checks=(_check(E.merged, E.root, E.child),),
        timeout=_NATIVE_TIMEOUT,
    )
    assert (
        result.report.verified
        and result.source is not None
        and result.patch is not None
    )
    output = result.source.graph(str(E.merged))
    shared = output.value(E.root, E.child)
    assert isinstance(shared, BNode) and shared == output.value(E.other, E.child)
    assert (
        shared,
        E.value,
        Literal("01", datatype=XSD.integer, normalize=False),
    ) in result.source.graph(shared)
    assert {
        tuple(term_key(term) for term in triple)
        for triple in result.source.graph(str(E.unrelated))
    } == {
        tuple(term_key(term) for term in triple)
        for triple in source.graph(str(E.unrelated))
    }
    names = {graph.identifier for graph in result.source.dataset().graphs()}
    assert E.new_empty in names and not names.intersection((E.G, E.H, E.old_empty))
    assert frozenset(source.dataset().quads()) == original
    assert export_migration(result, format="trig").content is None
    assert export_migration(result, accept=True, format="nquads").content is None


def test_subject_and_predicate_partition_leaves_unrelated_quads_and_inventory() -> None:
    source = _source()
    result = migrate_graphs(
        source,
        (GraphRoute(E.G, E.selected, subjects=(E.root,), predicates=(E.child,)),),
        added_graphs=(E.selected,),
        before_checks=(_check(E.G, E.root, E.child),),
        after_checks=(_check(E.selected, E.root, E.child),),
        timeout=_NATIVE_TIMEOUT,
    )
    assert (
        result.report.verified
        and result.source is not None
        and result.patch is not None
    )
    assert (
        E.keep,
        E.note,
        Literal("unchanged", datatype=XSD.string),
    ) in result.source.graph(str(E.G))
    assert not tuple(result.source.graph(str(E.G)).objects(E.root, E.child))
    assert set(result.patch.removed_graphs) == set()
    assert set(result.patch.added_graphs) == {E.selected}
    assert len(result.patch.removals) == len(result.patch.additions) == 1


def test_collisions_undeclared_scope_and_nonempty_graph_removal_reject() -> None:
    source = _source()
    cases = (
        (migrate_graphs(source, (GraphRoute(E.G, E.H),)), "rdf.graph_route_collision"),
        (migrate_graphs(source, (GraphRoute(E.G, E.new),)), "rdf.graph_route_scope"),
        (
            migrate_graphs(
                source,
                (GraphRoute(E.G, E.H, subjects=(E.root,)),),
                merge=True,
                removed_graphs=(E.G,),
            ),
            "rdf.graph_inventory_conflict",
        ),
        (
            migrate_graphs(
                source, (GraphRoute(E.G, E.H), GraphRoute(E.G, E.unrelated)), merge=True
            ),
            "rdf.graph_route_collision",
        ),
    )
    for result, code in cases:
        assert result.source is None and not result.report.verified
        assert any(issue.code == code for issue in result.report.issues)
    owned = cast(BNode, source.graph(str(E.G)).value(E.root, E.child))
    foreign = migrate_graphs(
        source, (GraphRoute(E.G, E.G, subjects=(BNode(str(owned)),)),)
    )
    assert foreign.source is None and not foreign.report.verified


def test_graph_local_failure_and_missing_contracts_block_acceptance() -> None:
    source = _source()
    result = migrate_graphs(
        source,
        (GraphRoute(E.G, E.selected, subjects=(E.root,)),),
        added_graphs=(E.selected,),
        before_checks=(_check(E.G, E.root, E.child),),
        after_checks=(_check(E.G, E.root, E.child),),
        timeout=_NATIVE_TIMEOUT,
    )
    assert result.source is not None and not result.report.verified
    assert result.before[0].verified and result.after[0].outcome == "disagreement"
    assert export_migration(result, accept=True, format="trig").content is None
    unchecked = migrate_graphs(source, (GraphRoute(E.G, E.G),), timeout=_NATIVE_TIMEOUT)
    assert unchecked.source is not None and not unchecked.report.verified


def test_authorized_pinned_scope_union_bounds_cancellation_and_fresh_recovery() -> None:
    dataset = _source().dataset()
    pinned = prepare_rdf(RdfSource(dataset, graph_name=str(E.G)))
    unchanged = migrate_graphs(
        pinned,
        (GraphRoute(E.G, E.G),),
        before_checks=(_check(E.G, E.root, E.child),),
        after_checks=(_check(E.G, E.root, E.child),),
        timeout=_NATIVE_TIMEOUT,
    )
    assert unchanged.source is not None and unchanged.report.verified
    assert str(unchanged.source.graph_name) == str(E.G)
    assert E.H not in {
        graph.identifier for graph in unchanged.source.dataset().graphs()
    }
    assert (
        migrate_graphs(pinned, (GraphRoute(E.G, E.H),), added_graphs=(E.H,)).source
        is None
    )
    union = prepare_rdf(RdfSource(dataset, union=True))
    assert migrate_graphs(union, (GraphRoute(E.G, E.G),)).source is None
    for limits in (Limits(max_work=1), Limits(max_bytes=10), Limits(max_states=1)):
        limited = migrate_graphs(
            _source(), (GraphRoute(E.G, E.G),), budget=Budget(limits)
        )
        assert limited.source is None and limited.report.outcome == "exhausted"
    cancel = threading.Event()
    cancel.set()
    stopped = migrate_graphs(_source(), (GraphRoute(E.G, E.G),), cancel=cancel)
    assert stopped.source is None and stopped.report.outcome == "exhausted"
    assert migrate_graphs(
        _source(),
        (GraphRoute(E.G, E.G),),
        before_checks=(_check(E.G, E.root, E.child),),
        after_checks=(_check(E.G, E.root, E.child),),
        timeout=_NATIVE_TIMEOUT,
    ).report.verified
