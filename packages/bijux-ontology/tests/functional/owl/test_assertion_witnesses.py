"""Sound finite witnesses distinguish individual identity from RDF spelling."""

import threading

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.owl.assertions import check_assertion_witnesses
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.terms import literal_term, term_key

E = Namespace("urn:witnesses:")


def _object(graph: Graph, predicate: URIRef = E.p) -> Graph:
    """Declare logical object properties without conflating other punned roles."""
    return graph.add((predicate, RDF.type, OWL.ObjectProperty))


def test_functional_identity_requires_actual_difference_or_type_clash() -> None:
    """Different names alone supply no contradiction; proofs retain premises."""
    graph = _object(Graph()).add((E.p, RDF.type, OWL.FunctionalProperty))
    graph.add((E.a, E.p, E.b)).add((E.a, E.p, E.c))
    equal = check_assertion_witnesses(graph)
    assert equal.report.verified and equal.status == "no_witness_in_selected_scope"
    assert any(item.rule == "functional" for item in equal.equalities)
    graph.add((E.b, OWL.differentFrom, E.c))
    conflict = check_assertion_witnesses(graph)
    assert (
        conflict.status == "contradiction_found"
        and conflict.report.outcome == "disagreement"
    )
    witness = next(
        item for item in conflict.witnesses if item.rule == "different_individuals"
    )
    original = {tuple(term_key(term) for term in triple) for triple in graph}
    assert set(witness.premises) <= original and len(witness.premises) >= 4
    graph.remove((E.b, OWL.differentFrom, E.c))
    graph.add((E.b, RDF.type, E.Left)).add((E.c, RDF.type, E.Right))
    graph.add((E.Left, OWL.disjointWith, E.Right))
    typed = check_assertion_witnesses(graph)
    assert any(item.rule == "disjoint_types" for item in typed.witnesses)


def test_irreflexive_asymmetric_disjoint_negative_and_inverse_functional() -> None:
    """Each characteristic supplies original positive/negative premise witnesses."""
    graph = _object(Graph()).add((E.p, RDF.type, OWL.IrreflexiveProperty))
    graph.add((E.a, E.p, E.b))
    assert check_assertion_witnesses(graph).report.verified
    graph.add((E.a, E.p, E.a))
    assert any(
        item.rule == "irreflexive"
        for item in check_assertion_witnesses(graph).witnesses
    )
    graph.remove((E.a, E.p, E.a)).add((E.p, RDF.type, OWL.AsymmetricProperty))
    graph.add((E.b, E.p, E.a))
    assert any(
        item.rule == "asymmetric" for item in check_assertion_witnesses(graph).witnesses
    )
    graph = _object(_object(Graph()), E.q)
    graph.add((E.p, OWL.propertyDisjointWith, E.q))
    graph.add((E.a, E.p, E.b)).add((E.a, E.q, E.b))
    assert any(
        item.rule == "disjoint_properties"
        for item in check_assertion_witnesses(graph).witnesses
    )
    negative = BNode()
    for predicate, value in (
        (RDF.type, OWL.NegativePropertyAssertion),
        (OWL.sourceIndividual, E.a),
        (OWL.assertionProperty, E.p),
        (OWL.targetIndividual, E.b),
    ):
        graph.add((negative, predicate, value))
    assert any(
        item.rule == "negative_assertion"
        for item in check_assertion_witnesses(graph).witnesses
    )
    graph = _object(Graph()).add((E.p, RDF.type, OWL.InverseFunctionalProperty))
    graph.add((E.a, E.p, E.shared)).add((E.b, E.p, E.shared))
    graph.add((E.a, OWL.differentFrom, E.b))
    result = check_assertion_witnesses(graph)
    assert result.witnesses and any(
        item.rule == "inverse_functional" for item in result.equalities
    )


def _key_graph() -> Graph:
    """The common object value witnesses a key without equal complete value sets."""
    graph = _object(Graph())
    head = BNode()
    Collection(graph, head, [E.p])
    graph.add((E.Kind, OWL.hasKey, head))
    graph.add((E.a, RDF.type, E.Kind)).add((E.b, RDF.type, E.Kind))
    graph.add((E.a, E.p, E.shared)).add((E.a, E.p, E.extra)).add((E.b, E.p, E.shared))
    return graph


def test_keys_share_values_with_named_witnesses() -> None:
    """Keys need named witnesses, including actual equality to a name."""
    graph = _key_graph()
    result = check_assertion_witnesses(graph)
    assert result.report.verified and any(
        item.rule == "key" for item in result.equalities
    )
    assert not any(
        {item.left, item.right} == {term_key(E.shared), term_key(E.extra)}
        for item in result.equalities
    )
    graph.remove((E.b, E.p, E.shared))
    missing = check_assertion_witnesses(graph)
    assert missing.report.verified and not missing.equalities
    graph = _key_graph()
    blank = BNode()
    graph.remove((None, E.p, E.shared))
    graph.add((E.a, E.p, blank)).add((E.b, E.p, blank))
    anonymous = check_assertion_witnesses(graph)
    assert anonymous.status == "incomplete" and not anonymous.equalities
    graph.add((E.a, E.p, E.known)).add((E.b, E.p, E.known))
    alternative = check_assertion_witnesses(graph)
    assert alternative.report.verified and any(
        item.rule == "key" for item in alternative.equalities
    )
    graph.remove((None, E.p, E.known))
    graph.add((blank, OWL.sameAs, E.named))
    named = check_assertion_witnesses(graph)
    assert named.report.verified and any(
        item.rule == "key" for item in named.equalities
    )
    universal = _key_graph()
    head = universal.value(E.Kind, OWL.hasKey)
    universal.remove((E.Kind, OWL.hasKey, head))
    universal.remove((None, RDF.type, E.Kind))
    assert head is not None
    universal.add((OWL.Thing, OWL.hasKey, head))
    assert any(
        item.rule == "key" for item in check_assertion_witnesses(universal).equalities
    )
    # One independent shared witness per component is sufficient, even when the
    # complete asserted sets differ on both components.
    graph = _key_graph()
    head = graph.value(E.Kind, OWL.hasKey)
    graph.remove((E.Kind, OWL.hasKey, head))
    head = BNode()
    Collection(graph, head, [E.p, E.q])
    graph.add((E.Kind, OWL.hasKey, head)).add((E.q, RDF.type, OWL.DatatypeProperty))
    graph.add((E.a, E.q, literal_term("01", datatype=str(XSD.integer))))
    graph.add((E.b, E.q, literal_term("1", datatype=str(XSD.integer))))
    graph.add((E.a, E.q, literal_term("2", datatype=str(XSD.integer))))
    assert any(
        item.rule == "key" for item in check_assertion_witnesses(graph).equalities
    )


def test_datatype_values_preserve_lexical_terms_and_unknowns() -> None:
    """OWL value identity differs from spelling; unsupported stays unknown."""
    graph = (
        Graph()
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.p, RDF.type, OWL.FunctionalProperty))
    )
    first = literal_term("01", datatype=str(XSD.integer))
    graph.add((E.a, E.p, first)).add(
        (E.a, E.p, literal_term("1", datatype=str(XSD.integer)))
    )
    result = check_assertion_witnesses(graph)
    assert result.report.verified and not result.witnesses
    assert str(first) == "01" and len(graph) == 4
    graph.add((E.a, E.p, literal_term("2", datatype=str(XSD.integer))))
    conflict = check_assertion_witnesses(graph)
    assert any(item.rule == "functional_data" for item in conflict.witnesses)
    graph = (
        Graph()
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.p, RDF.type, OWL.FunctionalProperty))
    )
    graph.add((E.a, E.p, Literal("same", datatype=E.Custom, normalize=False)))
    graph.add((E.a, E.p, Literal("other", datatype=E.Custom, normalize=False)))
    assert check_assertion_witnesses(graph).status == "incomplete"
    ordinary = Graph().add((E.p, RDF.type, OWL.DatatypeProperty))
    ordinary.add((E.a, E.p, Literal("unknown", datatype=E.Custom, normalize=False)))
    assert check_assertion_witnesses(ordinary).status == "incomplete"
    negative = BNode()
    ordinary.remove((E.a, E.p, None))
    for predicate, value in (
        (RDF.type, OWL.NegativePropertyAssertion),
        (OWL.sourceIndividual, E.a),
        (OWL.assertionProperty, E.p),
        (OWL.targetValue, Literal("unknown", datatype=E.Custom)),
    ):
        ordinary.add((negative, predicate, value))
    assert check_assertion_witnesses(ordinary).status == "incomplete"
    ordinary.remove((negative, None, None))
    ordinary.remove((E.a, E.p, None))
    ordinary.add((E.a, E.p, literal_term("bad", datatype=str(XSD.integer))))
    invalid = check_assertion_witnesses(ordinary)
    lexical = next(
        item for item in invalid.witnesses if item.rule == "datatype_lexical"
    )
    assert (
        tuple(term_key(term) for term in (E.p, RDF.type, OWL.DatatypeProperty))
        in lexical.premises
    )
    graph = (
        Graph()
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.p, RDF.type, OWL.FunctionalProperty))
    )
    graph.add((E.a, E.p, Literal("same"))).add(
        (E.a, E.p, Literal("same", datatype=XSD.string))
    )
    assert check_assertion_witnesses(graph).report.verified


def test_selected_graph_union_pins_cancel_and_prefix_limits_are_honest() -> None:
    """Selected scope preserves hidden graphs and retained witness prefixes."""
    dataset = Dataset()
    _object(dataset.graph(E.g)).add((E.p, RDF.type, OWL.FunctionalProperty))
    dataset.graph(E.g).add((E.a, E.p, E.b)).add((E.a, E.p, E.c))
    dataset.graph(E.h).add((E.b, OWL.differentFrom, E.c))
    source = prepare_rdf(dataset)
    with pytest.raises(InputError):
        check_assertion_witnesses(source)
    assert check_assertion_witnesses(source, graph_name=str(E.g)).report.verified
    assert check_assertion_witnesses(source, union=True).witnesses
    cancel = threading.Event()
    cancel.set()
    assert (
        check_assertion_witnesses(source, union=True, cancel=cancel).report.outcome
        == "exhausted"
    )
    graph = _object(Graph()).add((E.p, RDF.type, OWL.IrreflexiveProperty))
    graph.add((E.a, E.p, E.a)).add((E.b, E.p, E.b))
    bounded = check_assertion_witnesses(graph, limits=Limits(max_issues=1))
    assert (
        bounded.status == "contradiction_found"
        and bounded.report.outcome == "exhausted"
    )
    assert any(
        issue.code == "owl.assertion_contradiction" for issue in bounded.report.issues
    )
