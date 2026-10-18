"""Owned graph selection preserves exact IRI meaning and bounds existing names."""

import tracemalloc

import pytest
from rdflib import Dataset, Literal, Namespace, URIRef

from bijux_ontology import InputError, Limits, RdfSource, plan_rdf_patch, prepare_source

E = Namespace("urn:graph-scope:")


def test_pinned_graph_accepts_equal_iri_text_and_keeps_conflicting_scope_closed() -> (
    None
):
    dataset = Dataset()
    dataset.graph(E.G).add((E.record, E.value, Literal("selected")))
    dataset.graph(E.H).add((E.record, E.value, Literal("hidden")))
    source = prepare_source(RdfSource(dataset, graph_name=str(E.G)))
    assert set(source.graph(E.G)) == set(source.graph(str(E.G)))
    with pytest.raises(InputError, match="conflicts"):
        source.graph(E.H)


def test_prepared_graph_names_exhaust_before_large_temporary_copies() -> None:
    # Construct inputs before tracing: the operation must bound already owned
    # names before maintained dataset iteration or URIRef comparisons copy them.
    name = URIRef("urn:graph-scope:" + "g" * 1_000_000)
    dataset = Dataset()
    dataset.graph(name)
    sources = tuple(
        prepare_source(RdfSource(dataset, graph_name=selected))
        for selected in (None, str(name))
    )
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    try:
        for source in sources:
            start, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            result = plan_rdf_patch(
                source, source, limits=Limits(max_bytes=1000, max_total_bytes=10000)
            )
            _, peak = tracemalloc.get_traced_memory()
            assert result.patch is None and result.report.outcome == "exhausted"
            assert peak - start < len(name) // 4
    finally:
        if not tracing:
            tracemalloc.stop()
