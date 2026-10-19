"""Actual named-individual families preserve open-world and literal value semantics."""

import os
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from bijux_ontology_reasoning.axioms.types import (
    DataProperty,
    NamedEntity,
    ObjectProperty,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import prepare_owl_document
from bijux_ontology_reasoning.inference.individuals import (
    IndividualFamily,
    IndividualQuery,
    RealizationGroup,
)
from bijux_ontology_reasoning.inference.sessions import (
    PreparedReasoningTheory,
    ReasoningSession,
    prepare_reasoning_theory,
)
from rdflib import URIRef
from rdflib.namespace import RDF

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

_AXIOMS = """
Declaration(Class(<urn:Parent>)) Declaration(Class(<urn:Child>))
Declaration(Class(<urn:Exists>)) Declaration(Class(<urn:NotParent>))
Declaration(ObjectProperty(<urn:p>)) Declaration(ObjectProperty(<urn:q>))
Declaration(ObjectProperty(<urn:inv>)) Declaration(ObjectProperty(<urn:f>))
Declaration(DataProperty(<urn:dp>)) Declaration(DataProperty(<urn:dq>))
Declaration(DataProperty(<urn:equiv>)) Declaration(DataProperty(<urn:text>))
Declaration(NamedIndividual(<urn:alice>)) Declaration(NamedIndividual(<urn:alias>))
Declaration(NamedIndividual(<urn:bob>)) Declaration(NamedIndividual(<urn:bobAlias>))
Declaration(NamedIndividual(<urn:carol>)) Declaration(NamedIndividual(<urn:free>))
Declaration(NamedIndividual(<urn:lonely>))
SubClassOf(<urn:Child> <urn:Parent>) DisjointClasses(<urn:Parent> <urn:NotParent>)
ClassAssertion(<urn:NotParent> <urn:carol>)
SubClassOf(<urn:Exists> ObjectSomeValuesFrom(<urn:p> <urn:Parent>))
SubObjectPropertyOf(<urn:p> <urn:q>) InverseObjectProperties(<urn:p> <urn:inv>)
ObjectPropertyRange(<urn:p> <urn:Parent>) FunctionalObjectProperty(<urn:f>)
SubDataPropertyOf(<urn:dp> <urn:dq>) EquivalentDataProperties(<urn:dq> <urn:equiv>)
ClassAssertion(<urn:Child> <urn:alice>) ClassAssertion(<urn:Exists> <urn:lonely>)
ObjectPropertyAssertion(<urn:p> <urn:alice> <urn:bob>)
ObjectPropertyAssertion(<urn:f> <urn:free> <urn:bob>)
ObjectPropertyAssertion(<urn:f> <urn:free> <urn:bobAlias>)
SameIndividual(<urn:alice> <urn:alias>)
DataPropertyAssertion(<urn:dp> <urn:alice> "01"^^<http://www.w3.org/2001/XMLSchema#integer>)
DataPropertyAssertion(<urn:dp> <urn:alice> "1"^^<http://www.w3.org/2001/XMLSchema#integer>)
DataPropertyAssertion(<urn:text> <urn:alice> "hello"@en)
"""


def _entity(name: str, *, cls: bool = False) -> NamedEntity:
    """Use exact named roles, without a label or prefix resolver."""
    return NamedEntity("class" if cls else "named_individual", "urn:" + name)


def _names(groups: tuple[RealizationGroup, ...]) -> set[str]:
    """Read actual returned named members without asserting maximal equality groups."""
    return {value.iri for group in groups for value in group.members}


def _theory(backend: OwlProfileBackend, extra: str = "") -> PreparedReasoningTheory:
    """Capture original native syntax, preserving exact typed literal spellings."""
    return prepare_reasoning_theory(
        backend,
        prepare_owl_document(
            "Ontology(<urn:realization> " + _AXIOMS + extra + ")",
            format="functional",
            document_iri="urn:realization:source",
        ),
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require an explicitly pinned real backend instead of a protocol substitute."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned native OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_all_six_native_families_and_direct_depth_share_one_classification(
    backend: OwlProfileBackend,
) -> None:
    theory = _theory(backend)
    queries = (
        IndividualQuery("types", _entity("alias"), direct=False),
        IndividualQuery("instances", _entity("Parent", cls=True), direct=False),
        IndividualQuery("object_values", _entity("alias"), ObjectProperty("urn:q")),
        IndividualQuery("data_values", _entity("alias"), DataProperty("urn:equiv")),
        IndividualQuery("same", _entity("bob")),
        IndividualQuery("different", _entity("alias")),
    )
    with ReasoningSession(theory) as session:
        result = session.realize(queries)
        assert result.report.verified and result.consistent is True
        types, instances, objects, data, same, different = result.answers
        assert {"urn:Child", "urn:Parent"} <= _names(types.types)
        assert {"urn:alice", "urn:alias", "urn:bob", "urn:bobAlias"} <= _names(
            instances.instances
        )
        assert _names(objects.object_values) == {"urn:bob", "urn:bobAlias"}
        assert {(v.lexical, v.datatype, v.language) for v in data.data_values} == {
            ("01", "http://www.w3.org/2001/XMLSchema#integer", None),
            ("1", "http://www.w3.org/2001/XMLSchema#integer", None),
        }
        assert same.same_individuals is not None
        assert {v.iri for v in same.same_individuals.members} == {
            "urn:bob",
            "urn:bobAlias",
        }
        assert _names(different.different_individuals) == {"urn:carol"}
        assert all(answer.node_policy == "BY_NAME" for answer in result.answers)
        assert all(len(group.members) == 1 for group in objects.object_values)
        direct = session.realize(
            (
                IndividualQuery("types", _entity("alias"), direct=True),
                IndividualQuery("instances", _entity("Parent", cls=True), direct=True),
            )
        )
        assert direct.report.verified
        assert _names(direct.answers[0].types) == {"urn:Child"}
        assert _names(direct.answers[1].instances) == {"urn:bob", "urn:bobAlias"}
        assert session.classifications == 1 and session.launches == 1
        assert result.theory_identity == theory.identity
        assert result.documents == theory.documents


def test_open_world_inverse_language_and_no_named_existential_witness(
    backend: OwlProfileBackend,
) -> None:
    with ReasoningSession(_theory(backend)) as session:
        result = session.realize(
            (
                IndividualQuery("different", _entity("free")),
                IndividualQuery(
                    "object_values", _entity("lonely"), ObjectProperty("urn:p")
                ),
                IndividualQuery(
                    "object_values", _entity("bob"), ObjectProperty("urn:p", True)
                ),
                IndividualQuery(
                    "data_values", _entity("alias"), DataProperty("urn:text")
                ),
            )
        )
    assert result.report.verified
    assert not result.answers[0].different_individuals
    assert not result.answers[1].object_values
    assert _names(result.answers[2].object_values) == {"urn:alice", "urn:alias"}
    (literal,) = result.answers[3].data_values
    assert literal.lexical == "hello" and literal.language == "en"


def test_data_has_value_cannot_hide_incomplete_getter_behind_other_families(
    backend: OwlProfileBackend,
) -> None:
    extra = (
        "SubClassOf(<urn:Child> DataHasValue(<urn:dp> "
        '"2"^^<http://www.w3.org/2001/XMLSchema#integer>))'
    )
    with ReasoningSession(_theory(backend, extra)) as session:
        result = session.realize(
            (
                IndividualQuery(
                    "data_values", _entity("alias"), DataProperty("urn:dq")
                ),
                IndividualQuery("types", _entity("alias"), direct=False),
                IndividualQuery("same", _entity("bob")),
            )
        )
    assert not result.report.verified and not result.report.complete
    assert not result.answers[0].report.verified
    assert not result.answers[0].data_values
    assert result.answers[1].report.verified and result.answers[2].report.verified


def test_invalid_fresh_roles_and_inconsistent_theory_never_return_empty_green(
    backend: OwlProfileBackend,
) -> None:
    with ReasoningSession(_theory(backend)) as session:
        invalid = session.realize(
            (IndividualQuery(cast(IndividualFamily, "missing"), _entity("alias")),)
        )
        assert invalid.report.outcome == "input_error"
        for query in (
            IndividualQuery("same", _entity("Missing")),
            IndividualQuery("same", _entity("Parent")),
            IndividualQuery(
                "object_values", _entity("alias"), ObjectProperty("urn:dp")
            ),
            IndividualQuery("data_values", _entity("alias"), DataProperty("urn:q")),
        ):
            result = session.realize((query,))
            assert not result.report.verified and not result.report.complete
        assert session.realize(
            (IndividualQuery("same", _entity("bob")),)
        ).report.verified
    with ReasoningSession(
        _theory(backend, "ClassAssertion(<urn:NotParent> <urn:alice>)")
    ) as session:
        inconsistent = session.realize((IndividualQuery("same", _entity("bob")),))
    assert inconsistent.consistent is False and not inconsistent.report.verified
    assert inconsistent.consistency_report.outcome == "disagreement"
    assert not inconsistent.answers


def test_bounded_partial_native_node_cancel_and_shared_budget_recovery(
    backend: OwlProfileBackend,
) -> None:
    with ReasoningSession(_theory(backend)) as session:
        query = IndividualQuery("same", _entity("bob"))
        stopped = session.realize((query,), max_results=1)
        assert stopped.report.outcome == "exhausted" and not stopped.report.complete
        node = stopped.answers[0].same_individuals
        assert node is not None and len(node.members) == 1 and not node.complete
        assert session.realize((query,)).report.verified
        event = Event()
        event.set()
        assert session.realize((query,), cancel=event).report.outcome == "exhausted"
        assert session.realize((query,)).report.verified
        shared = Budget()
        shared.consume("work", shared.remaining("work"))
        assert session.realize((query,), budget=shared).report.outcome == "exhausted"


def test_exact_selected_rdf_and_import_scope_remains_distinct_from_assertions(
    backend: OwlProfileBackend,
) -> None:
    source = prepare_rdf(
        RdfSource(
            """@prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <urn:selected> { <urn:source> a owl:Ontology; owl:imports <urn:import> .
          <urn:A> a owl:Class; rdfs:subClassOf <urn:B> .
          <urn:B> a owl:Class .
          <urn:i> a owl:NamedIndividual, <urn:A>; <urn:p> <urn:j> .
          <urn:j> a owl:NamedIndividual .
          <urn:p> a owl:ObjectProperty; rdfs:subPropertyOf <urn:q> .
          <urn:q> a owl:ObjectProperty . }
        <urn:excluded> { <urn:i> a <urn:OutOfScope> . }
        """,
            format="trig",
            graph_name="urn:selected",
            document_iri="urn:source",
        )
    )
    imported = RdfSource(
        """@prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        <urn:import> a owl:Ontology . <urn:B> a owl:Class; rdfs:subClassOf <urn:C> .
        <urn:C> a owl:Class .""",
        format="turtle",
        document_iri="urn:import",
    )
    assert (URIRef("urn:i"), RDF.type, URIRef("urn:B")) not in source.graph()
    assert (URIRef("urn:i"), URIRef("urn:q"), URIRef("urn:j")) not in source.graph()
    theory = prepare_reasoning_theory(backend, source, imports={"urn:import": imported})
    with ReasoningSession(theory) as session:
        result = session.realize(
            (
                IndividualQuery("types", _entity("i"), direct=False),
                IndividualQuery("object_values", _entity("i"), ObjectProperty("urn:q")),
            )
        )
    assert result.report.verified
    assert {"urn:A", "urn:B", "urn:C"} <= _names(result.answers[0].types)
    assert "urn:OutOfScope" not in _names(result.answers[0].types)
    assert _names(result.answers[1].object_values) == {"urn:j"}
    assert result.rdf_sources == theory.rdf_sources
    assert result.rdf_sources[0] is source
    assert result.documents[0] == theory.documents[0]
