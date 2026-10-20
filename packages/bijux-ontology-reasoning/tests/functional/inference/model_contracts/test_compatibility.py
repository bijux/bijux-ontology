"""Actual automatic model entailment, possible types and finite co-emission."""

import os
from dataclasses import replace
from pathlib import Path

import pytest
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.inference.datatypes.types import DatatypeFieldBranch
from bijux_ontology_reasoning.inference.model_contracts import check_model_compatibility
from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.sources import RdfSource

_BASE = """Ontology(<urn:models>
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
 Declaration(ObjectProperty(<urn:p>)) Declaration(ObjectProperty(<urn:q>))
 Declaration(ObjectProperty(<urn:r>)) Declaration(ObjectProperty(<urn:u>))
 Declaration(DataProperty(<urn:d>))
 SubClassOf(<urn:A> <urn:B>)
 SubClassOf(<urn:A> ObjectSomeValuesFrom(<urn:p> <urn:C>))
 SubClassOf(<urn:A> ObjectMinCardinality(1 <urn:p> <urn:C>))
 DisjointClasses(<urn:B> <urn:D>)
 ObjectPropertyDomain(<urn:q>
   ObjectIntersectionOf(<urn:B> ObjectSomeValuesFrom(<urn:p> <urn:C>)))
 ObjectPropertyRange(<urn:q> <urn:C>)
 ObjectPropertyDomain(<urn:r> <urn:D>) ObjectPropertyRange(<urn:r> <urn:C>)
 ObjectPropertyDomain(<urn:u> <urn:C>) ObjectPropertyRange(<urn:u> <urn:C>)
 DataPropertyRange(<urn:d> <http://www.w3.org/2001/XMLSchema#decimal>)
)"""


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use an explicitly selected actual pinned backend, never runtime discovery."""
    java, directory, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
        )
    )
    if not (java and directory and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def source(text: str = _BASE) -> OwlDocument:
    """Capture original native source bytes without a projection or synthetic theory."""
    return prepare_owl_document(text, format="functional", document_iri="urn:source")


def field(name: str, *, guaranteed: bool = False) -> FieldDeclaration:
    """Use actual existing field contracts and original diagnostic anchors."""
    return FieldDeclaration(
        name,
        "urn:" + name,
        (ValueDeclaration("iri", types=("urn:C",)),),
        location="payload." + name,
        emission="guaranteed" if guaranteed else "optional",
    )


def test_automatic_anonymous_subsumption_and_distinct_possible_types(
    backend: OwlProfileBackend,
) -> None:
    """Resolve an anonymous endpoint; mere possibility cannot satisfy it."""
    original = source()
    compatible = ModelDeclaration(
        "sample", ("urn:A",), (field("q"),), location="schema.sample"
    )
    accepted = check_model_compatibility(backend, original, (compatible,))
    assert accepted.report.verified and accepted.consistent is True
    required = next(
        answer
        for answer in accepted.answers
        if answer.question.field == "q" and answer.question.branch is None
    )
    assert required.raw_entailed is True and required.raw_satisfiable is None
    assert required.question.location == "payload.q" and required.required is not None
    conflicting = check_model_compatibility(
        backend, original, (replace(compatible, types=("urn:B",)),)
    )
    possible = next(
        answer
        for answer in conflicting.answers
        if answer.question.kind == "possible_satisfiability"
        and answer.question.field is None
    )
    assert possible.raw_satisfiable is True and possible.raw_entailed is None
    assert not conflicting.report.verified
    assert any(
        issue.code == "models.required_subsumption_not_entailed"
        and issue.location == "payload.q"
        for issue in conflicting.report.issues
    )
    assert original.content == _BASE.encode()


def test_explicit_mutual_exclusion_and_guaranteed_coemission(
    backend: OwlProfileBackend,
) -> None:
    """Only actual declared promises make an impossible co-emission a finding."""
    fields = (field("q"), field("r"))
    exclusive = EmissionGroup(
        "choice",
        ("q", "r"),
        minimum=1,
        maximum=1,
        scenarios=(
            EmissionScenario("first", ("q",)),
            EmissionScenario("second", ("r",)),
        ),
        closed=True,
    )
    model = ModelDeclaration(
        "choice",
        ("http://www.w3.org/2002/07/owl#Thing",),
        fields,
        location="schema.choice",
        emission_groups=(exclusive,),
    )
    accepted = check_model_compatibility(backend, source(), (model,))
    assert accepted.report.verified
    together = EmissionGroup(
        "together",
        ("q", "r"),
        minimum=2,
        maximum=2,
        scenarios=(EmissionScenario("both", ("q", "r")),),
        closed=True,
    )
    refused = check_model_compatibility(
        backend, source(), (replace(model, emission_groups=(together,)),)
    )
    assert not refused.report.verified
    conflict = next(
        answer
        for answer in refused.answers
        if answer.question.kind == "coemission_contradiction"
    )
    assert conflict.raw_satisfiable is False and conflict.question.guaranteed
    assert any(
        issue.code == "models.logical_coemission_conflict"
        and issue.location == "schema.choice"
        for issue in refused.report.issues
    )
    uncertain = check_model_compatibility(
        backend, source(), (replace(model, emission_groups=()),)
    )
    assert not uncertain.report.verified
    assert any(
        issue.code == "models.logical_conditional_conflict_unverified"
        for issue in uncertain.report.issues
    )


def test_three_field_state_is_not_reduced_to_pairs(backend: OwlProfileBackend) -> None:
    """A genuinely three-way class contradiction is checked as one finite state."""
    text = _BASE.replace(
        "DisjointClasses(<urn:B> <urn:D>)",
        "DisjointClasses(ObjectIntersectionOf(<urn:B> <urn:C>) <urn:D>)",
    )
    group = EmissionGroup(
        "three",
        ("q", "r", "u"),
        minimum=3,
        maximum=3,
        scenarios=(EmissionScenario("all", ("q", "r", "u")),),
        closed=True,
    )
    model = ModelDeclaration(
        "three",
        ("http://www.w3.org/2002/07/owl#Thing",),
        (field("q"), field("r"), field("u")),
        emission_groups=(group,),
    )
    result = check_model_compatibility(backend, source(text), (model,))
    state = next(
        answer
        for answer in result.answers
        if answer.question.kind == "coemission_contradiction"
    )
    assert state.question.present == ("q", "r", "u") and state.raw_satisfiable is False
    assert not result.report.verified


def test_owl_existence_never_establishes_required_output(
    backend: OwlProfileBackend,
) -> None:
    """Native existential/cardinality truth cannot stand in for emitted predicates."""
    required = replace(
        field("p", guaranteed=True), output=OutputRequirement(minimum_values=1)
    )
    model = ModelDeclaration("emission", ("urn:A",), (required,))
    logical = check_model_compatibility(backend, source(), (model,))
    assert logical.report.verified
    graph = Graph()
    graph.add((URIRef("urn:record"), RDF.type, URIRef("urn:A")))
    actual = check_rdf_emission(
        prepare_source(graph),
        (model,),
        (RdfModelUsage("emission", URIRef("urn:record")),),
    )
    assert not actual.verified
    assert any(
        "count" in issue.code or "required" in issue.code or "missing" in issue.code
        for issue in actual.issues
    )


def test_literal_branches_delegate_to_actual_datatype_leaf(
    backend: OwlProfileBackend,
) -> None:
    """One class endpoint and one literal range retain their separate native answers."""
    literal = FieldDeclaration(
        "d",
        "urn:d",
        (
            ValueDeclaration(
                "literal", datatype="http://www.w3.org/2001/XMLSchema#integer"
            ),
        ),
        location="payload.d",
        emission="optional",
    )
    model = ModelDeclaration("mixed", ("urn:A",), (field("q"), literal))
    result = check_model_compatibility(backend, source(), (model,))
    assert result.report.verified and len(result.datatypes) == 1
    answer = result.datatypes[0].answers[0]
    assert isinstance(answer.request, DatatypeFieldBranch)
    assert answer.outcome == "included" and answer.request.field.name == "d"
    assert answer.request.branch == 0 and answer.request.field.location == "payload.d"
    assert answer.request.field is result.models[0].fields[1]


def test_unsupported_context_and_inconsistent_source_stay_unknown(
    backend: OwlProfileBackend,
) -> None:
    """Unexecuted supported remainder cannot disappear behind a positive question."""
    model = ModelDeclaration(
        "list",
        ("urn:A",),
        (
            replace(
                field("q"),
                values=(ValueDeclaration("iri", types=("urn:C",), container="@list"),),
            ),
        ),
    )
    unknown = check_model_compatibility(backend, source(), (model,))
    assert not unknown.report.verified
    assert any(answer.native_error is not None for answer in unknown.answers)
    scenario = EmissionScenario(
        "selected", ("q",), EmissionCondition("q", URIRef("urn:unestablished"))
    )
    conditioned = ModelDeclaration(
        "conditional",
        ("http://www.w3.org/2002/07/owl#Thing",),
        (field("q"),),
        emission_groups=(
            EmissionGroup(
                "choice",
                ("q",),
                minimum=1,
                maximum=1,
                scenarios=(scenario,),
                closed=True,
            ),
        ),
    )
    conditional = check_model_compatibility(backend, source(), (conditioned,))
    assert not conditional.report.verified
    assert any(
        issue.code == "models.logical_scenario_unverified"
        for issue in conditional.report.issues
    )
    uncertain_pair = replace(
        conditioned,
        fields=(field("q"), field("r")),
        emission_groups=(
            EmissionGroup(
                "pair",
                ("q", "r"),
                minimum=2,
                maximum=2,
                scenarios=(
                    EmissionScenario("selected", ("q", "r"), scenario.condition),
                ),
                closed=True,
            ),
        ),
    )
    conditional_conflict = check_model_compatibility(
        backend, source(), (uncertain_pair,)
    )
    assert conditional_conflict.report.outcome == "unverified"
    assert any(
        answer.raw_satisfiable is False for answer in conditional_conflict.answers
    )
    inconsistent = source(
        _BASE[:-1] + " Declaration(NamedIndividual(<urn:conflict>)) "
        "ClassAssertion(<urn:B> <urn:conflict>) "
        "ClassAssertion(<urn:D> <urn:conflict>))"
    )
    failed = check_model_compatibility(backend, inconsistent, (model,))
    assert failed.raw_consistent is False and failed.consistent is False
    assert not failed.report.verified and all(
        answer.raw_entailed is None and answer.raw_satisfiable is None
        for answer in failed.answers
    )


def test_one_owned_rdf_closure_binds_class_and_literal_reasoning(
    backend: OwlProfileBackend,
) -> None:
    """Capture mutable imported physical scope once for both actual reasoner calls."""
    root = prepare_source(
        RdfSource(
            "<urn:root> a <http://www.w3.org/2002/07/owl#Ontology>; "
            "<http://www.w3.org/2002/07/owl#imports> <urn:import> .",
            format="turtle",
            document_iri="urn:root",
        )
    )
    imported = Dataset()
    selected = imported.graph(URIRef("urn:selected"))
    selected.parse(
        data="""@prefix : <urn:> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:import a owl:Ontology . :A a owl:Class; rdfs:subClassOf :B .
:B a owl:Class . :C a owl:Class .
:q a owl:ObjectProperty; rdfs:domain :B; rdfs:range :C .
:d a owl:DatatypeProperty;
 rdfs:range <http://www.w3.org/2001/XMLSchema#decimal> .""",
        format="turtle",
    )
    imported.graph(URIRef("urn:shadow")).parse(
        data="<urn:A> <http://www.w3.org/2000/01/rdf-schema#subClassOf> "
        "<http://www.w3.org/2002/07/owl#Nothing> .",
        format="turtle",
    )
    literal = FieldDeclaration(
        "d",
        "urn:d",
        (
            ValueDeclaration(
                "literal", datatype="http://www.w3.org/2001/XMLSchema#integer"
            ),
        ),
        emission="optional",
    )
    result = check_model_compatibility(
        backend,
        root,
        (ModelDeclaration("rdf", ("urn:A",), (field("q"), literal)),),
        imports={
            "urn:import": RdfSource(
                imported, document_iri="urn:import", graph_name="urn:selected"
            )
        },
    )
    assert result.report.verified and result.datatypes[0].report.verified
    assert len(result.rdf_sources) == 2 and result.rdf_sources[0] is root
    captured = result.rdf_sources[1]
    assert captured.graph_name == "urn:selected" and captured.is_dataset
    original_size = len(captured.graph())
    selected.remove((None, None, None))
    assert len(captured.graph()) == original_size and original_size > 0
    assert result.datatypes[0].answers[0].outcome == "included"
