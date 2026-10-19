"""Owned graph selection preserves exact IRI meaning and bounds existing names."""

import tracemalloc

import pytest
from rdflib import Dataset, Literal, Namespace, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.patches import plan_rdf_patch
from bijux_ontology.rdf.sources.types import RdfSource

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


def test_warm_pinned_reader_requalifies_effective_graph_metadata() -> None:
    name = "urn:graph-scope:" + "g" * 100_000
    dataset = Dataset()
    dataset.graph(URIRef(name)).add((E.record, E.value, Literal("selected")))
    source = prepare_source(RdfSource(dataset, graph_name=name))
    assert len(source._read()) == 1
    with pytest.raises(LimitExceeded, match="max_bytes"):
        source._read(budget=Budget(Limits(max_bytes=1000)))
    assert len(source._read()) == 1
