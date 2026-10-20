"""Exact scoped set algebra keeps anonymous ownership and graph inventories."""

from typing import Literal as OperationName

import pytest
from rdflib import BNode, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.algebra import rdf_set_operation
from bijux_ontology.rdf.patches import BlankNodePair
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term

EX = Namespace("urn:algebra:")


def _graphs() -> PreparedSource:
    """Own one dataset with shared anonymous terms and unequal exact numerals."""
    data = Dataset()
    node = BNode("same")
    for name in (EX.left, EX.right):
        data.graph(name).add((EX.s, EX.p, node))
        data.graph(name).add((node, EX.label, literal_term("shared")))
    data.graph(EX.left).add(
        (node, EX.number, Literal("01", datatype=XSD.integer, normalize=False))
    )
    data.graph(EX.right).add(
        (node, EX.number, Literal("1", datatype=XSD.integer, normalize=False))
    )
    return prepare_rdf(data)


def _node(source: PreparedSource) -> BNode:
    """Export actual immutable term evidence from a selected owned graph."""
    node = source.graph(str(EX.left)).value(EX.s, EX.p)
    assert isinstance(node, BNode)
    return node


def test_same_owner_graph_intersection_and_exact_difference() -> None:
    """Graph projection shares anonymous identity without numeric normalization."""
    source = _graphs()
    before = frozenset(source.dataset().quads())
    result = rdf_set_operation(
        source, source, "intersection", left_graph=EX.left, right_graph=EX.right
    )
    assert result.report.verified and result.source is not None
    graph = result.source.graph()
    shared = graph.value(EX.s, EX.p)
    assert (
        isinstance(shared, BNode)
        and (shared, EX.label, literal_term("shared", datatype=str(XSD.string)))
        in graph
    )
    assert len(graph) == 2 and not result.source.is_dataset
    difference = rdf_set_operation(
        source, source, "difference", left_graph=EX.left, right_graph=EX.right
    )
    assert difference.report.verified and difference.source is not None
    assert [
        str(value) for value in difference.source.graph().objects(None, EX.number)
    ] == ["01"]
    assert frozenset(source.dataset().quads()) == before


def test_independent_inputs_require_owned_complete_correspondence() -> None:
    """Matching local labels or a foreign equal term never establish identity."""
    left, right = _graphs(), _graphs()
    unknown = rdf_set_operation(left, right, "intersection")
    assert unknown.report.outcome == "unverified" and unknown.source is None
    pairs = (BlankNodePair(_node(left), _node(right)),)
    result = rdf_set_operation(left, right, "intersection", blank_pairs=pairs)
    assert result.report.verified and result.source is not None
    assert len(result.source.dataset()) == len(left.dataset())
    foreign = BNode(str(_node(left)))
    rejected = rdf_set_operation(
        left, right, "intersection", blank_pairs=(BlankNodePair(foreign, _node(right)),)
    )
    assert rejected.report.outcome == "unverified" and rejected.source is None
    with pytest.raises(InputError):
        rdf_set_operation(left, right, "intersection", blank_pairs=(*pairs, *pairs))
    named = rdf_set_operation(left, right, "intersection", named_only=True)
    assert named.report.verified and named.source is not None
    assert not named.source.dataset()
    assert set(named.source._graph_names()) == {EX.left, EX.right}


def test_dataset_inventory_operations_keep_empty_and_required_graphs() -> None:
    """Empty graphs are set members; retained quads keep their physical graph."""
    first, second = Dataset(), Dataset()
    first.graph(EX.left)
    second.graph(EX.right)
    for data in (first, second):
        data.graph(EX.common)
        data.graph(EX.populated).add((EX.s, EX.p, Literal("shared")))
    first.graph(EX.populated).add((EX.s, EX.p, Literal("left")))
    left, right = prepare_rdf(first), prepare_rdf(second)
    expected: dict[
        OperationName["intersection", "difference", "symmetric_difference"], set[URIRef]
    ] = {
        "intersection": {EX.common, EX.populated},
        "difference": {EX.left, EX.populated},
        "symmetric_difference": {EX.left, EX.right, EX.populated},
    }
    for operation, names in expected.items():
        result = rdf_set_operation(left, right, operation)
        assert result.report.verified and result.source is not None
        assert set(result.source._graph_names()) == names
        values = set(result.source.graph(str(EX.populated)).objects(EX.s, EX.p))
        assert values == {
            literal_term(
                "shared" if operation == "intersection" else "left",
                datatype=str(XSD.string),
            )
        }


def test_scope_and_exhaustion_withhold_partial_candidates() -> None:
    """Missing identity fidelity and interrupted work cannot publish a set result."""
    source = _graphs()
    mismatch = rdf_set_operation(source, source, "intersection", left_graph=EX.left)
    assert mismatch.source is None and mismatch.report.outcome == "unverified"
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=1),
        Limits(max_triples=1),
    ):
        result = rdf_set_operation(source, source, "difference", limits=limits)
        assert result.source is None and result.report.outcome == "exhausted"
    partial = PreparedSource(
        source.dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source_terms",
        graph_fidelity="nonempty-only",
    )
    result = rdf_set_operation(partial, partial, "difference")
    assert result.source is None and result.report.outcome == "unverified"
    dataset = Dataset()
    dataset.graph(BNode("graph")).add((EX.s, EX.p, Literal("value")))
    selected = prepare_rdf(dataset)
    actual = next(selected._graph_names())
    assert isinstance(actual, BNode)
    good = rdf_set_operation(
        selected, selected, "intersection", left_graph=actual, right_graph=actual
    )
    assert good.report.verified
    foreign = BNode(str(actual))
    bad = rdf_set_operation(
        selected, selected, "intersection", left_graph=foreign, right_graph=foreign
    )
    assert bad.source is None and bad.report.outcome == "unverified"
    for selector in (Literal(str(EX.left)), URIRef("@default")):
        with pytest.raises(InputError):
            rdf_set_operation(source, source, "intersection", left_graph=selector)
