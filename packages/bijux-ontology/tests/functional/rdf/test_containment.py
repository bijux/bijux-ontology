"""Actual finite simple entailment, exact physical scope and coherent witnesses."""

import threading

from rdflib import BNode, Dataset, Graph, Namespace
from rdflib.namespace import RDF, RDFS, XSD

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.containment import check_simple_entailment
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import literal_term

E = Namespace("urn:containment:")


def test_existential_witnesses_are_coherent_and_distinct_variables_may_coalesce() -> (
    None
):
    blank, first, second = BNode(), BNode(), BNode()
    data = prepare_rdf(
        Graph()
        .add((blank, E.p, E.value))
        .add((E.s, E.q, blank))
        .add((E.extra, E.p, E.other))
    )
    template = prepare_rdf(
        Graph()
        .add((E.s, E.q, first))
        .add((first, E.p, E.value))
        .add((second, E.p, E.value))
    )
    result = check_simple_entailment(data, template)
    assert result.report.verified and result.entailed is True
    assert len(result.bindings) == 2
    owned_data = next(data.graph().objects(E.s, E.q))
    owned_template = set(template.graph().subjects(E.p, E.value))
    assert all(binding.value is owned_data for binding in result.bindings)
    assert {binding.variable for binding in result.bindings} == owned_template
    split = prepare_rdf(Graph().add((E.s, E.q, E.left)).add((E.right, E.p, E.value)))
    refused = check_simple_entailment(split, template)
    assert refused.entailed is False and refused.report.outcome == "disagreement"
    assert not refused.bindings


def test_ground_terms_keep_exact_lexical_identity_and_no_inferred_schema() -> None:
    exact = literal_term("01", datatype=str(XSD.integer))
    other = literal_term("1", datatype=str(XSD.integer))
    data = prepare_rdf(
        Graph()
        .add((E.s, E.p, exact))
        .add((E.Child, RDFS.subClassOf, E.Parent))
        .add((E.s, RDF.type, E.Child))
    )
    assert check_simple_entailment(
        data, prepare_rdf(Graph().add((E.s, E.p, exact)))
    ).report.verified
    different = check_simple_entailment(
        data, prepare_rdf(Graph().add((E.s, E.p, other)))
    )
    assert different.entailed is False
    inferred = check_simple_entailment(
        data, prepare_rdf(Graph().add((E.s, RDF.type, E.Parent)))
    )
    assert inferred.entailed is False
    empty = check_simple_entailment(data, prepare_rdf(Graph()))
    assert empty.report.verified and empty.entailed is True and not empty.bindings


def test_physical_named_scope_does_not_turn_into_union() -> None:
    dataset = Dataset()
    dataset.graph(E.first).add((E.s, E.p, E.value))
    dataset.graph(E.second).add((E.s, E.q, E.other))
    data = prepare_rdf(dataset)
    template = prepare_rdf(Graph().add((E.s, E.p, E.value)).add((E.s, E.q, E.other)))
    result = check_simple_entailment(data, template, data_graph=str(E.first))
    assert result.entailed is False and result.report.complete
    one = prepare_rdf(Graph().add((E.s, E.p, E.value)))
    assert check_simple_entailment(data, one, data_graph=str(E.first)).report.verified
    assert check_simple_entailment(data, one, data_graph="@default").entailed is False
    union = prepare_rdf(RdfSource(dataset, union=True))
    unsupported = check_simple_entailment(union, one)
    assert unsupported.entailed is None
    assert unsupported.report.outcome == "unverified"


def test_exhaustion_and_cancel_do_not_establish_absence() -> None:
    data = prepare_rdf(Graph().add((E.s, E.p, E.value)))
    for kind in ("work", "states", "triples", "total_bytes"):
        budget = Budget(Limits(**{"max_" + kind: 1}))
        result = check_simple_entailment(data, data, budget=budget)
        assert result.entailed is None and not result.bindings
        assert any(issue.kind == "exhausted" for issue in result.report.issues)
    cancel = threading.Event()
    cancel.set()
    result = check_simple_entailment(data, data, cancel=cancel)
    assert result.entailed is None and result.report.outcome == "exhausted"
    assert check_simple_entailment(data, data).report.verified
