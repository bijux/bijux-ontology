"""Actual complex-expression class retrieval shares one prepared HermiT theory."""

import os
from pathlib import Path
from threading import Event
from typing import cast

import pytest
from bijux_ontology_reasoning.axioms.types import (
    ClassComplement,
    ClassExpression,
    ClassJunction,
    NamedEntity,
    ObjectCardinality,
    ObjectProperty,
    ObjectQuantifier,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.inference.classes import ClassQueryResult
from bijux_ontology_reasoning.inference.sessions import (
    ReasoningSession,
    prepare_reasoning_theory,
)
from rdflib import URIRef
from rdflib.namespace import RDFS

from bijux_ontology.limits import Budget
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

_SOURCE = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<urn:classes> a owl:Ontology .
<urn:B> a owl:Class . <urn:C> a owl:Class . <urn:Other> a owl:Class .
<urn:p> a owl:ObjectProperty .
<urn:A> a owl:Class; rdfs:subClassOf <urn:B>,
 [a owl:Restriction; owl:onProperty <urn:p>; owl:someValuesFrom <urn:C>].
<urn:D> a owl:Class; rdfs:subClassOf <urn:A> .
<urn:Equivalent> a owl:Class; owl:equivalentClass
 [a owl:Class; owl:intersectionOf (<urn:B>
  [a owl:Restriction; owl:onProperty <urn:p>; owl:someValuesFrom <urn:C>])].
<urn:Alias> a owl:Class; owl:equivalentClass <urn:Equivalent> .
<urn:Bad> a owl:Class; rdfs:subClassOf
 [a owl:Class; owl:intersectionOf (<urn:B> [a owl:Class; owl:complementOf <urn:B>])].
"""


def _class(name: str) -> NamedEntity:
    """Preserve the explicit class role without label lookup or prefix guessing."""
    return NamedEntity("class", "urn:" + name)


def _expression() -> ClassJunction:
    """Ask for a class intersection with an actual existential condition."""
    return ClassJunction(
        "intersection",
        (_class("B"), ObjectQuantifier("some", ObjectProperty("urn:p"), _class("C"))),
    )


def _members(result: ClassQueryResult) -> set[str]:
    """Read actual retained nodes without implying an asserted hierarchy edge."""
    return {member.iri for group in result.subclasses for member in group.members}


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require the ordinary installed pinned native runtime supplied by the caller."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_inferred_complex_subclasses_equivalents_and_depth_reuse_classification(
    backend: OwlProfileBackend,
) -> None:
    source = prepare_rdf(RdfSource(_SOURCE, format="turtle"))
    asserted = source.graph()
    assert set(asserted.objects(URIRef("urn:D"), RDFS.subClassOf)) == {URIRef("urn:A")}
    assert URIRef("urn:Equivalent") not in asserted.objects(
        URIRef("urn:A"), RDFS.subClassOf
    )
    theory = prepare_reasoning_theory(backend, source)
    with ReasoningSession(theory) as session:
        result = session.query_classes(_expression())
        direct = session.query_classes(_expression(), direct=True)
        assert result.report.verified and direct.report.verified
        assert {"urn:A", "urn:D"} <= _members(result)
        assert "urn:A" in _members(direct) and "urn:D" not in _members(direct)
        assert result.equivalents is not None and result.equivalents.complete
        assert {member.iri for member in result.equivalents.members} == {
            "urn:Equivalent",
            "urn:Alias",
        }
        assert result.satisfiable is True and result.consistent is True
        assert (
            result.theory_identity == theory.identity
            and result.documents == theory.documents
        )
        assert session.classifications == 1 and session.launches == 1


def test_unsatisfiable_equivalence_is_separate_from_consistent_theory(
    backend: OwlProfileBackend,
) -> None:
    theory = prepare_reasoning_theory(
        backend, prepare_rdf(RdfSource(_SOURCE, format="turtle"))
    )
    with ReasoningSession(theory) as session:
        impossible = ClassJunction(
            "intersection", (_class("B"), ClassComplement(_class("B")))
        )
        result = session.query_classes(impossible)
    assert result.report.verified and result.consistent is True
    assert result.satisfiable is False and result.equivalents is not None
    assert result.equivalents.bottom and {
        member.iri for member in result.equivalents.members
    } >= {"urn:Bad", "http://www.w3.org/2002/07/owl#Nothing"}
    assert not result.subclasses


def test_unsupported_strings_fresh_roles_and_inconsistent_inputs_never_empty_green(
    backend: OwlProfileBackend,
) -> None:
    theory = prepare_reasoning_theory(
        backend, prepare_rdf(RdfSource(_SOURCE, format="turtle"))
    )
    with ReasoningSession(theory) as session:
        for expression in (
            cast(ClassExpression, "B and p some C"),
            _class("Missing"),
            _class("p"),
            ObjectCardinality("min", 1, ObjectProperty("urn:p"), _class("C"), True),
        ):
            result = session.query_classes(expression)
            assert not result.report.verified and not result.report.complete
        assert session.query_classes(_expression()).report.verified
        assert session.classifications == 1
    inconsistent = _SOURCE + "\n<urn:individual> a <urn:Bad> ."
    theory = prepare_reasoning_theory(
        backend, prepare_rdf(RdfSource(inconsistent, format="turtle"))
    )
    with ReasoningSession(theory) as session:
        result = session.query_classes(_expression())
    assert result.consistent is False and not result.report.verified
    assert not result.report.complete and not result.subclasses


def test_real_bounded_node_prefix_cancel_and_fresh_recovery(
    backend: OwlProfileBackend,
) -> None:
    theory = prepare_reasoning_theory(
        backend, prepare_rdf(RdfSource(_SOURCE, format="turtle"))
    )
    with ReasoningSession(theory) as session:
        stopped = session.query_classes(_expression(), max_results=1)
        assert stopped.report.outcome == "exhausted" and not stopped.report.complete
        assert stopped.equivalents is not None and len(stopped.equivalents.members) == 1
        assert not stopped.equivalents.complete
        assert session.query_classes(_expression()).report.verified
        event = Event()
        event.set()
        cancelled = session.query_classes(_expression(), cancel=event)
        assert cancelled.report.outcome == "exhausted"
        assert session.query_classes(_expression()).report.verified
        shared = Budget()
        shared.consume("work", shared.remaining("work"))
        exhausted = session.query_classes(_expression(), budget=shared)
        assert exhausted.report.outcome == "exhausted"
