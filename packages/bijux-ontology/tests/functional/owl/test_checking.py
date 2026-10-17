"""Selected OWL encodings retain scope and semantic-support distinctions."""

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.vocabulary import Vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology

EX = Namespace("https://example.org/")


def test_owl_conflicts_use_selected_closure_and_preserve_legal_punning() -> None:
    local = Graph().add((EX.p, RDF.type, OWL.DatatypeProperty))
    local.add((EX.p, RDF.type, OWL.FunctionalProperty))
    local.add((EX.Class, RDF.type, OWL.Class))
    local.add((EX.Class, RDF.type, OWL.NamedIndividual))
    lookup = Graph().add((EX.p, RDF.type, OWL.ObjectProperty))
    view = Vocabulary(local, lookup=(lookup,))
    assert check_ontology(view, regime="owl-dl", families=("declarations",)).verified
    conflict = check_ontology(
        view, regime="owl-dl", families=("declarations",), include_lookup=True
    )
    assert conflict.issues[0].code == "ontology.declaration_conflict"
    assert (
        "local" in conflict.issues[0].location
        and "lookup[0]" in conflict.issues[0].location
    )
    assert check_ontology(
        view, regime="rdf", families=("declarations",), include_lookup=True
    ).verified
    local.add((EX.p, RDF.type, OWL.TransitiveProperty))
    characteristic = check_ontology(local, regime="owl-dl", families=("declarations",))
    assert [issue.code for issue in characteristic.issues] == [
        "ontology.characteristic_conflict"
    ]


def test_expression_shapes_and_legal_lexical_cardinality_remain_unverified() -> None:
    graph = Graph()
    graph.add((EX.valid, RDF.type, OWL.Restriction))
    graph.add((EX.valid, OWL.onProperty, EX.p))
    graph.add(
        (
            EX.valid,
            OWL.minCardinality,
            Literal("+01", datatype=XSD.nonNegativeInteger, normalize=False),
        )
    )
    report = check_ontology(graph, families=("expressions",))
    assert [issue.code for issue in report.issues] == ["ontology.expression_unverified"]
    graph.add((EX.missing, RDF.type, OWL.Restriction))
    graph.add((EX.qualified, OWL.onProperty, EX.p))
    graph.add(
        (
            EX.qualified,
            OWL.qualifiedCardinality,
            Literal(1, datatype=XSD.nonNegativeInteger),
        )
    )
    graph.add((EX.union, OWL.unionOf, RDF.nil))
    report = check_ontology(graph, families=("expressions",))
    codes = {issue.code for issue in report.issues}
    assert {
        "ontology.restriction_shape",
        "ontology.qualified_cardinality_shape",
        "ontology.expression_arity",
    } <= codes
    assert not report.verified


def test_group_lists_are_checked_and_cyclic_or_literal_members_fail() -> None:
    graph = Graph()
    graph.add((EX.group, RDF.type, OWL.AllDisjointClasses))
    head = BNode()
    Collection(graph, head, [EX.A, Literal("invalid class")])
    graph.add((EX.group, OWL.members, head))
    assert any(
        issue.code == "ontology.expression_operand"
        for issue in check_ontology(graph, families=("expressions",)).issues
    )
    graph.remove((None, RDF.rest, RDF.nil))
    tail = next(graph.objects(head, RDF.rest))
    graph.add((tail, RDF.rest, head))
    assert any(
        issue.code == "ontology.expression_list"
        for issue in check_ontology(graph, families=("expressions",)).issues
    )
    missing = Graph().add((EX.group, RDF.type, OWL.AllDifferent))
    assert (
        check_ontology(missing, families=("expressions",)).issues[0].code
        == "ontology.group_shape"
    )


def test_headers_and_annotations_require_their_actual_structural_components() -> None:
    graph = Graph()
    graph.add((EX.ontology, RDF.type, OWL.Ontology))
    graph.add((EX.ontology, OWL.versionIRI, EX.version))
    graph.add((EX.other, RDF.type, OWL.Ontology))
    graph.add((EX.A, RDFS.subClassOf, EX.B))
    axiom = BNode()
    for predicate, value in (
        (RDF.type, OWL.Axiom),
        (OWL.annotatedSource, EX.A),
        (OWL.annotatedProperty, RDFS.subClassOf),
        (OWL.annotatedTarget, EX.B),
    ):
        graph.add((axiom, predicate, value))
    assert check_ontology(graph, families=("headers", "annotations")).verified
    graph.remove((EX.A, RDFS.subClassOf, EX.B))
    graph.add((EX.ontology, OWL.versionIRI, Literal("not an IRI")))
    assert {
        issue.code
        for issue in check_ontology(graph, families=("headers", "annotations")).issues
    } == {
        "ontology.version_ambiguous",
        "ontology.version_shape",
        "ontology.annotation_statement_missing",
    }
