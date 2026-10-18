"""Native OBO subset metadata and qualified ordinary application membership."""

import importlib.util

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.obo.subsets import read_obo_subsets
from bijux_ontology.vocabulary.view import Vocabulary

EX = Namespace("https://example.org/obo/")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")


def _definition() -> Graph:
    """Use the published OBO-in-OWL subsetdef and inSubset representation."""
    return (
        Graph()
        .add((EX.slim, RDF.type, OWL.AnnotationProperty))
        .add((EX.slim, RDFS.subPropertyOf, OIO.SubsetProperty))
        .add((EX.slim, RDFS.label, Literal("slim")))
        .add((EX.slim, RDFS.comment, Literal("Selected terms", lang="en")))
    )


def _model(enumeration: object) -> ModelDeclaration:
    """Bind explicit RDF intent through the established enumeration contract."""
    from bijux_ontology.declarations.enumerations import ValueEnumeration

    assert isinstance(enumeration, ValueEnumeration)
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "choice",
                str(EX.choice),
                (
                    ValueDeclaration(
                        "iri", constraints=ValueConstraints(enumeration=enumeration)
                    ),
                ),
            ),
        ),
    )


def test_published_definition_original_metadata_and_actual_allowed_value_checks() -> (
    None
):
    """Native output uses original subset members without SKOS or module claims."""
    graph = _definition().add((EX.A, OIO.inSubset, EX.slim))
    view = inspect_vocabulary(graph)
    graph.remove((None, None, None))
    inventory = read_obo_subsets(view, closed=True)
    assert inventory.report.verified
    assert inventory.definitions[0].iri == EX.slim
    assert any(
        edge.object == Literal("Selected terms", lang="en")
        for edge in inventory.definitions[0].metadata
    )
    assert not any(
        edge.object == SKOS.ConceptScheme for edge in inventory.definitions[0].metadata
    )
    allowed = inventory.select(str(EX.slim))
    assert allowed.qualified and allowed.values == (EX.A,)
    model = _model(allowed)
    for value, valid in ((EX.A, True), (EX.outside, False)):
        data = prepare_rdf(
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.choice, value))
        )
        report = check_rdf_emission(
            data, (model,), (RdfModelUsage("record", EX.record),)
        )
        assert report.verified is valid
        assert report.outcome == ("agreement" if valid else "disagreement")
        assert allowed.report in report.children


def test_closed_undefined_invalid_and_empty_memberships_are_explicit() -> None:
    """A finite closed reference error retains the original statement and source."""
    graph = _definition()
    graph.add((EX.A, OIO.inSubset, EX.undefined))
    graph.add((EX.B, OIO.inSubset, Literal("slim")))
    inventory = read_obo_subsets(inspect_vocabulary(graph), closed=True)
    assert inventory.report.outcome == "disagreement"
    assert len(inventory.memberships) == 2
    issues = {issue.code: issue for issue in inventory.report.issues}
    assert issues["obo.subset_undefined"].location == str(EX.A)
    assert issues["obo.subset_undefined"].details is not None
    assert issues["obo.subset_undefined"].details.source == "local"
    assert issues["obo.subset_membership_kind"].location == str(EX.B)
    empty = read_obo_subsets(inspect_vocabulary(_definition()), closed=True).select(
        str(EX.slim)
    )
    assert empty.qualified and empty.values == ()
    assert (
        application_matches(ValueConstraints(enumeration=empty), EX.A, budget=Budget())
        is False
    )
    absent = read_obo_subsets(inspect_vocabulary(Graph()), closed=True).select(
        str(EX.slim)
    )
    assert not absent.qualified and absent.report is not None
    assert any(issue.code == "obo.subset_undefined" for issue in absent.report.issues)


def test_open_scope_imports_and_anonymous_members_preserve_uncertainty() -> None:
    """Known membership survives open scope, while missing names stay unknown."""
    local = Graph().add((EX.A, OIO.inSubset, EX.slim))
    imported = _definition().add((EX.A, OIO.inSubset, EX.slim))
    vocabulary = Vocabulary(local, imports=(imported,))
    inventory = read_obo_subsets(inspect_vocabulary(vocabulary), closed=True)
    assert inventory.report.verified and len(inventory.memberships) == 2
    assert {edge.scope for edge in inventory.memberships} == {"local", "import[0]"}
    assert inventory.select(str(EX.slim)).values == (EX.A,)
    local_only = read_obo_subsets(
        inspect_vocabulary(vocabulary, scopes=("local",)), closed=True
    )
    assert local_only.report.outcome == "disagreement"
    opened = read_obo_subsets(inspect_vocabulary(vocabulary)).select(str(EX.slim))
    constraints = ValueConstraints(enumeration=opened)
    assert not opened.qualified
    assert application_matches(constraints, EX.A, budget=Budget()) is True
    assert application_matches(constraints, EX.outside, budget=Budget()) is None
    blank = BNode()
    imported.add((blank, OIO.inSubset, EX.slim))
    anonymous = read_obo_subsets(inspect_vocabulary(imported), closed=True)
    assert any(isinstance(edge.subject, BNode) for edge in anonymous.memberships)
    assert not anonymous.select(str(EX.slim)).qualified


def test_definition_kinds_and_partial_work_never_certify_closure() -> None:
    """Missing role evidence differs from a known conflicting property kind."""
    graph = _definition().add((EX.A, OIO.inSubset, EX.slim))
    graph.remove((EX.slim, RDF.type, OWL.AnnotationProperty))
    missing = read_obo_subsets(inspect_vocabulary(graph), closed=True)
    assert missing.report.outcome == "unverified"
    graph.add((EX.slim, RDF.type, OWL.ObjectProperty))
    assert (
        read_obo_subsets(inspect_vocabulary(graph), closed=True).report.outcome
        == "disagreement"
    )
    view = inspect_vocabulary(_definition().add((EX.A, OIO.inSubset, EX.slim)))
    for cap in (1, 10, 100):
        inventory = read_obo_subsets(
            view, closed=True, budget=Budget(Limits(max_work=cap))
        )
        assert inventory.report.outcome == "exhausted"
        assert not inventory.select(str(EX.slim)).qualified
    ordinary = read_obo_subsets(view, closed=True)
    bounded = ordinary.select(
        "https://example.org/" + "x" * 100_000, budget=Budget(Limits(max_bytes=1000))
    )
    assert bounded.report is not None and bounded.report.outcome == "exhausted"
    assert all(issue.location == "" for issue in bounded.report.issues)
    with pytest.raises(InputError):
        ordinary.select(BNode("https://example.org/slim"))


def test_actual_serializer_once_uses_the_same_qualified_subset() -> None:
    """An ordinary serializer feeds checked actual JSON-LD, with no adapter engine."""
    if importlib.util.find_spec("pyld") is None:
        pytest.skip("Actual JSON-LD output checking requires [jsonld]")
    from bijux_ontology.models.emission import ModelUsage
    from bijux_ontology.models.emission.serialization import check_instance

    enumeration = read_obo_subsets(
        inspect_vocabulary(_definition().add((EX.A, OIO.inSubset, EX.slim))),
        closed=True,
    ).select(str(EX.slim))
    model = _model(enumeration)
    vocabulary = (
        Graph()
        .add((EX.choice, RDF.type, OWL.ObjectProperty))
        .add((EX.Record, RDF.type, OWL.Class))
    )
    calls: list[object] = []

    def serialize(value: object) -> dict[str, object]:
        """Return one actual consumer record from the supplied value."""
        calls.append(value)
        return {"@type": str(EX.Record), "choice": str(value)}

    for chosen, valid in ((EX.A, True), (EX.outside, False)):
        calls.clear()
        result = check_instance(
            chosen,
            serialize,
            (model,),
            (ModelUsage("record"),),
            vocabulary=vocabulary,
            options={},
            context={"choice": {"@id": str(EX.choice), "@type": "@id"}},
        )
        assert calls == [chosen]
        assert result.output == {"@type": str(EX.Record), "choice": str(chosen)}
        assert result.report.verified is valid
