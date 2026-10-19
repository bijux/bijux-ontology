"""Narrow native proofs preserve independent unknown model obligations."""

from dataclasses import FrozenInstanceError

import pytest
from rdflib import BNode, Graph, Namespace
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.models.contracts.checking import assess_models, check_models
from bijux_ontology.owl.obligations import OntologyObligation, prove_obligations

EX = Namespace("https://example.org/")


def ontology() -> Graph:
    """Declare an independently provable named domain and datatype range."""
    graph = Graph()
    for cls in (EX.Child, EX.Parent, EX.Unrelated):
        graph.add((cls, RDF.type, OWL.Class))
    graph.add((EX.Child, RDFS.subClassOf, EX.Parent))
    graph.add((EX.name, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.name, RDFS.domain, EX.Parent))
    graph.add((EX.name, RDFS.range, XSD.string))
    graph.add((EX.other, RDF.type, OWL.ObjectProperty))
    return graph


def model() -> ModelDeclaration:
    """Require string output whose parent class may carry unsupported restrictions."""
    return ModelDeclaration(
        "record",
        (str(EX.Child),),
        (
            FieldDeclaration(
                "name",
                str(EX.name),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="guaranteed",
            ),
        ),
    )


@pytest.mark.parametrize("restriction", ["side", "relevant", "unrelated"])
def test_named_domain_proof_is_separate_from_restrictions_and_consistency(
    restriction: str,
) -> None:
    graph = ontology()
    node = BNode()
    owner = EX.Unrelated if restriction == "unrelated" else EX.Parent
    graph.add((owner, RDFS.subClassOf, node))
    graph.add((node, RDF.type, OWL.Restriction))
    graph.add(
        (node, OWL.onProperty, EX.name if restriction == "relevant" else EX.other)
    )
    graph.add(
        (
            node,
            OWL.allValuesFrom if restriction == "relevant" else OWL.someValuesFrom,
            XSD.integer if restriction == "relevant" else EX.Unrelated,
        )
    )
    required = OntologyObligation(
        "subsumption", (str(EX.Child),), str(EX.Parent), "record.name"
    )
    restrictions = OntologyObligation(
        "restrictions", (str(EX.Child),), location="record"
    )
    consistency = OntologyObligation("consistency")
    result = assess_models(
        (model(),), graph, obligations=(required, restrictions, consistency)
    )
    proof, coverage, global_consistency = result.obligations
    assert proof.status == "established" and proof.complete
    assert proof.witness == ((EX.Child, RDFS.subClassOf, EX.Parent),)
    assert coverage.status == (
        "established" if restriction == "unrelated" else "unknown"
    )
    assert (
        global_consistency.status == "unknown"
        and global_consistency.obligation == consistency
    )
    assert result.model_report.to_dict() == check_models((model(),), graph).to_dict()
    if restriction == "unrelated":
        assert result.model_report.verified and not result.outstanding
    else:
        assert not result.report.verified
        assert any(
            issue.code == "models.expression_unverified" for issue in result.outstanding
        )
        assert all(
            issue.code != "models.relationship_unverified"
            for issue in result.report.issues
        )
    with pytest.raises(FrozenInstanceError):
        proof.status = "unknown"  # type: ignore[misc]


def test_disjointness_does_not_establish_negative_subsumption_or_satisfiability() -> (
    None
):
    graph = ontology()
    graph.add((EX.Child, OWL.disjointWith, EX.Unrelated))
    questions = (
        OntologyObligation("disjointness", (str(EX.Child),), str(EX.Unrelated)),
        OntologyObligation("subsumption", (str(EX.Child),), str(EX.Unrelated)),
        OntologyObligation("required_coinstance", (str(EX.Child), str(EX.Unrelated))),
        OntologyObligation("subsumption", (str(EX.Parent),), str(EX.Unrelated)),
        OntologyObligation("required_coinstance", (str(EX.Parent),)),
    )
    answers = prove_obligations(graph, questions)
    assert [answer.status for answer in answers] == [
        "established",
        "unknown",
        "contradicted",
        "unknown",
        "unknown",
    ]
    assert all(answer.complete for answer in answers)
    assert (EX.Child, OWL.disjointWith, EX.Unrelated) in answers[0].witness
    assert answers[0].witness == answers[2].witness
    graph.add((EX.Child, RDFS.subClassOf, EX.Unrelated))
    disjoint, subsumption, impossible, *_ = prove_obligations(graph, questions)
    assert disjoint.status == subsumption.status == "established"
    assert impossible.status == "contradicted"


def test_equivalence_paths_and_disjoint_group_witnesses_contain_actual_edges() -> None:
    graph = ontology()
    graph.add((EX.Super, OWL.equivalentClass, EX.Parent))
    group, head = BNode(), BNode()
    graph.add((group, RDF.type, OWL.AllDisjointClasses))
    graph.add((group, OWL.members, head))
    Collection(graph, head, [EX.Super, EX.Unrelated])
    answers = prove_obligations(
        graph,
        (
            OntologyObligation("subsumption", (str(EX.Child),), str(EX.Super)),
            OntologyObligation("disjointness", (str(EX.Child),), str(EX.Unrelated)),
        ),
    )
    assert all(answer.status == "established" for answer in answers)
    assert answers[0].witness == (
        (EX.Child, RDFS.subClassOf, EX.Parent),
        (EX.Super, OWL.equivalentClass, EX.Parent),
    )
    assert answers[1].basis == "disjoint_group"
    assert all(edge in graph for answer in answers for edge in answer.witness)


@pytest.mark.parametrize(
    ("actual", "required", "status", "relation"),
    [
        (XSD.token, XSD.string, "established", "subset"),
        (XSD.integer, XSD.integer, "established", "equal"),
        (XSD.integer, XSD.string, "contradicted", "disjoint"),
        (XSD.integer, XSD.unsignedInt, "unknown", "overlap"),
        (EX.Custom, EX.Custom, "unknown", "unknown"),
    ],
)
def test_supported_datatype_inclusion_retains_its_independent_witness(
    actual: str, required: str, status: str, relation: str
) -> None:
    (result,) = prove_obligations(
        Graph(),
        (OntologyObligation("datatype_inclusion", (str(actual),), str(required)),),
    )
    assert result.status == status and result.datatype_relation == relation
    assert result.complete and result.basis == "datatype_relation"


def test_exhaustion_retains_proofs_and_marks_pending_answers_incomplete() -> None:
    questions = (
        OntologyObligation("subsumption", (str(EX.Child),), str(EX.Child)),
        OntologyObligation("subsumption", (str(EX.Child),), str(EX.Parent)),
        OntologyObligation("consistency"),
    )
    first, second, last = prove_obligations(
        ontology(), questions, limits=Limits(max_work=1)
    )
    assert first.status == "established" and first.complete
    assert second.status == last.status == "unknown"
    assert not second.complete and not last.complete
    assert second.basis == last.basis == "exhausted"


def test_selected_question_identity_rejects_conflated_or_missing_operands() -> None:
    with pytest.raises(InputError, match="Pair obligations"):
        OntologyObligation("subsumption", (str(EX.Child),))
    with pytest.raises(InputError, match="Only pair"):
        OntologyObligation("required_coinstance", (str(EX.Child),), str(EX.Parent))
    with pytest.raises(InputError, match="no selected class"):
        OntologyObligation("consistency", (str(EX.Child),))


def test_required_unknown_or_contradicted_answer_cannot_hide_behind_valid_model() -> (
    None
):
    graph = ontology()
    required = OntologyObligation("subsumption", (str(EX.Child),), str(EX.Unrelated))
    result = assess_models((model(),), graph, obligations=(required,))
    assert result.model_report.verified
    assert not result.report.verified
    assert result.report.outcome == "unverified"
    assert result.report.issues[-1].code == "ontology.obligation_unknown"
    assert not result.outstanding
    graph.add((EX.Child, OWL.disjointWith, EX.Unrelated))
    impossible = OntologyObligation(
        "required_coinstance", (str(EX.Child), str(EX.Unrelated))
    )
    contradicted = assess_models((model(),), graph, obligations=(impossible,))
    assert contradicted.model_report.verified
    assert contradicted.report.outcome == "disagreement"
    assert contradicted.report.issues[-1].code == "ontology.obligation_contradicted"
    established = assess_models(
        (model(),),
        graph,
        obligations=(
            OntologyObligation("subsumption", (str(EX.Child),), str(EX.Parent)),
        ),
    )
    assert established.model_report.verified and established.report.verified
