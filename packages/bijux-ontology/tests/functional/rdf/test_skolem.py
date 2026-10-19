"""Scoped fresh identity replacement and exact snapshot-bound reversal."""

from collections.abc import Iterator
from dataclasses import replace
from unittest.mock import patch

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.patches.types import BlankNodePair
from bijux_ontology.rdf.skolem.checking import deskolemize, skolemize
from bijux_ontology.rdf.skolem.types import SkolemMap
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

EX = "https://example.org/"
BASE = EX + ".well-known/genid/"
A, P, G, EMPTY = (URIRef(EX + name) for name in ("a", "p", "g", "empty"))


def _blank(source: PreparedSource) -> BNode:
    node = next(source._read().subjects())
    assert isinstance(node, BNode)
    return node


def test_dataset_sharing_empty_graphs_and_exact_literals_reverse() -> None:
    data = Dataset()
    shared, empty = BNode("shared"), BNode("empty")
    value = Literal("01", datatype=XSD.integer, normalize=False)
    data.graph(shared).add((shared, P, value))
    data.default_graph.add((A, P, shared))
    data.graph(G).add((shared, P, shared))
    data.graph(empty)
    data.graph(EMPTY)
    original = prepare_source(data)
    result = skolemize(original, BASE)
    assert (
        result.report.verified
        and result.source is not None
        and result.mapping is not None
    )
    assert len(result.mapping.bindings) == 2
    assert (
        result.mapping.original is original and result.mapping.output is result.source
    )
    binding = next(
        item
        for item in result.mapping.bindings
        if (item.iri, P, value) in result.source.graph(item.iri)
    )
    assert (A, P, binding.iri) in result.source.graph("@default")
    assert (binding.iri, P, binding.iri) in result.source.graph(G)
    assert len(result.source.graph(EMPTY)) == 0
    assert len(list(result.source.dataset().graphs())) == len(
        list(original.dataset().graphs())
    )
    assert not compare_rdf(original, result.source).verified
    restored = deskolemize(result.source, result.mapping)
    assert restored.report.verified and restored.source is not None
    assert compare_rdf(original, restored.source).verified
    actual = next(restored.source.graph(binding.original).subjects())
    assert actual is binding.original
    assert next(restored.source.graph(binding.original).objects()) is value


def test_invocations_are_fresh_and_explicit_owned_correspondence_reuses() -> None:
    first = prepare_source(Graph().add((BNode("same"), P, A)))
    second = prepare_source(Graph().add((BNode("same"), P, A)))
    one, two = skolemize(first, BASE), skolemize(second, BASE)
    assert one.mapping is not None and two.mapping is not None
    assert one.mapping.bindings[0].iri != two.mapping.bindings[0].iri
    linked = skolemize(
        second,
        BASE,
        shared=one.mapping,
        blank_pairs=(BlankNodePair(_blank(first), _blank(second)),),
    )
    assert linked.report.verified and linked.mapping is not None
    assert linked.mapping.bindings[0].iri == one.mapping.bindings[0].iri
    assert linked.source is not None
    restored = deskolemize(linked.source, linked.mapping)
    assert restored.source is not None and compare_rdf(second, restored.source).verified
    foreign = BNode(str(_blank(first)))
    assert foreign == _blank(first) and foreign is not _blank(first)
    rejected = skolemize(
        second,
        BASE,
        shared=one.mapping,
        blank_pairs=(BlankNodePair(foreign, _blank(second)),),
    )
    assert not rejected.report.verified and rejected.source is None
    duplicate = skolemize(
        second,
        BASE,
        shared=one.mapping,
        blank_pairs=(BlankNodePair(_blank(first), _blank(second)),) * 2,
    )
    assert duplicate.source is None and not duplicate.report.verified


def test_collisions_include_predicates_graphs_and_literal_datatypes() -> None:
    prior = prepare_source(Graph().add((BNode(), P, A)))
    result = skolemize(prior, BASE)
    assert result.mapping is not None
    iri = result.mapping.bindings[0].iri
    for position in ("subject", "predicate", "object", "graph", "datatype"):
        data = Dataset()
        node = BNode()
        data.default_graph.add((node, P, A))
        if position == "subject":
            data.default_graph.add((iri, P, A))
        elif position == "predicate":
            data.default_graph.add((A, iri, A))
        elif position == "object":
            data.default_graph.add((A, P, iri))
        elif position == "graph":
            data.graph(iri)
        else:
            data.default_graph.add((A, P, Literal("x", datatype=iri)))
        source = prepare_source(data)
        owned = next(
            node
            for node in source._read("@default").subjects(P, A)
            if isinstance(node, BNode)
        )
        assert isinstance(owned, BNode)
        rejected = skolemize(
            source,
            BASE,
            shared=result.mapping,
            blank_pairs=(BlankNodePair(_blank(prior), owned),),
        )
        assert rejected.source is None and not rejected.report.verified


def test_reversal_rejects_clones_other_maps_and_keeps_similar_named_iris() -> None:
    near = URIRef(BASE + "unrelated/0")
    original = prepare_source(Graph().add((BNode(), P, near)))
    first, second = skolemize(original, BASE), skolemize(original, BASE)
    assert (
        first.source is not None
        and first.mapping is not None
        and second.mapping is not None
    )
    clone = prepare_source(first.source.dataset())
    assert deskolemize(clone, first.mapping).source is None
    assert deskolemize(first.source, second.mapping).source is None
    restored = deskolemize(first.source, first.mapping)
    assert (
        restored.source is not None and next(restored.source.graph().objects()) == near
    )
    with pytest.raises(InputError):
        SkolemMap()
    with pytest.raises(TypeError):
        replace(first.mapping, output=clone)


def test_pinned_union_and_noop_scope_are_explicit() -> None:
    data = Dataset()
    node = BNode()
    data.graph(G).add((node, P, A))
    data.graph(EMPTY)
    data.default_graph.add((A, P, Literal("hidden")))
    pinned = prepare_source(RdfSource(data, graph_name=str(G)))
    result = skolemize(pinned, BASE)
    assert (
        result.report.verified
        and result.source is not None
        and result.mapping is not None
    )
    assert result.source.graph_name == str(G) and len(result.source.dataset()) == 1
    assert deskolemize(result.source, result.mapping).report.verified
    union = prepare_source(RdfSource(data, union=True))
    changed = skolemize(union, BASE)
    assert (
        changed.report.verified
        and changed.source is not None
        and changed.mapping is not None
    )
    assert changed.source.union and len(changed.source.dataset()) == 2
    assert len(list(changed.source.dataset().graphs())) == len(
        list(union.dataset().graphs())
    )
    restored = deskolemize(changed.source, changed.mapping)
    assert restored.source is not None and restored.source.union
    assert compare_rdf(union, restored.source).verified
    empty = skolemize(prepare_source(Dataset()), BASE)
    assert (
        empty.report.verified
        and empty.mapping is not None
        and not empty.mapping.bindings
    )


def test_bounds_metadata_and_cumulative_budget_do_not_publish_partial_maps() -> None:
    source = prepare_source(Graph().add((BNode(), P, A)))
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_total_bytes=1)):
        limited = skolemize(source, BASE, limits=limits)
        assert (
            limited.report.outcome == "exhausted"
            and limited.source is None
            and limited.mapping is None
        )
    oversized = skolemize(
        source,
        "https://example.org/" + "x" * 10000 + "/",
        limits=Limits(max_bytes=1000),
    )
    assert oversized.report.outcome == "exhausted" and all(
        len(issue.message) < 1000 for issue in oversized.report.issues
    )
    budget = Budget()
    completed = skolemize(source, BASE, budget=budget)
    assert completed.mapping is not None and completed.source is not None
    budget.consume("work", budget.remaining("work"))
    assert (
        deskolemize(completed.source, completed.mapping, budget=budget).report.outcome
        == "exhausted"
    )
    assert skolemize(source, "relative/").source is None
    assert skolemize(source, Literal(BASE)).source is None
    assert skolemize(source, BASE).report.verified


def test_input_triple_limits_stop_capture_and_union_does_not_build_a_cache() -> None:
    data = Dataset()
    for index in range(5000):
        data.graph(G).add((URIRef(EX + str(index)), P, BNode()))
    source = prepare_source(data)
    observed = 0
    iterate = Graph.__iter__

    def counted(graph: Graph) -> Iterator[tuple[Node, Node, Node]]:
        """Count actual maintained reads rather than inferred output size."""
        nonlocal observed
        for triple in iterate(graph):
            observed += 1
            yield triple

    with patch.object(Graph, "__iter__", counted):
        result = skolemize(source, BASE, limits=Limits(max_triples=1))
    assert result.report.outcome == "exhausted" and result.source is None
    assert observed <= 2
    union = prepare_source(RdfSource(data, union=True))
    assert not union._readers
    result = skolemize(union, BASE, limits=Limits(max_states=1))
    assert result.report.outcome == "exhausted" and result.source is None
    assert not union._readers


def test_map_rejects_mutated_snapshot_scope_origin_and_fidelity() -> None:
    """Object identity cannot authorize a later narrowed or reinterpreted owner."""
    other = URIRef(EX + "other")
    changes = (
        ("graph_name", str(G)),
        ("union", True),
        ("is_dataset", False),
        ("document_iri", EX + "changed-document"),
        ("effective_url", EX + "changed-effective"),
        ("literal_fidelity", "normalisation_allowed"),
        ("graph_fidelity", "nonempty-only"),
    )
    for owner in ("original", "output"):
        for name, changed in changes:
            data = Dataset()
            shared = BNode()
            data.graph(G).add((A, P, shared))
            data.graph(other).add((shared, P, A))
            original = prepare_source(data)
            named = skolemize(original, BASE)
            assert named.source is not None and named.mapping is not None
            assert deskolemize(named.source, named.mapping).report.verified
            prior_blank = named.mapping.bindings[0].original
            selected = getattr(named.mapping, owner)
            setattr(selected, name, changed)
            rejected = deskolemize(named.source, named.mapping)
            assert rejected.source is None and rejected.report.outcome == "unverified"
            next_source = prepare_source(Graph().add((BNode(), P, A)))
            reused = skolemize(
                next_source,
                BASE,
                shared=named.mapping,
                blank_pairs=(BlankNodePair(prior_blank, _blank(next_source)),),
            )
            assert reused.source is None and reused.mapping is None
            assert reused.report.outcome == "unverified"
            assert {g.identifier for g in original.dataset().graphs()} >= {G, other}
    for owner in ("original", "output"):
        source = prepare_source(Graph().add((BNode(), P, A)))
        named = skolemize(source, BASE)
        assert named.source is not None and named.mapping is not None
        selected = getattr(named.mapping, owner)
        selected.effective_url = "x" * 100000
        bounded = deskolemize(
            named.source, named.mapping, limits=Limits(max_bytes=1000)
        )
        assert bounded.source is None and bounded.report.outcome == "exhausted"
        future = prepare_source(Graph().add((BNode(), P, A)))
        reused = skolemize(
            future, BASE, shared=named.mapping, limits=Limits(max_bytes=1000)
        )
        assert reused.source is None and reused.report.outcome == "exhausted"
        assert all(
            len(issue.message) < 1000
            for issue in (*bounded.report.issues, *reused.report.issues)
        )
