"""Actual DL entailment retains explicit negation and open-world failures."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms import (
    ClassAssertion,
    ClassComplement,
    ClassJunction,
    Declaration,
    NamedEntity,
    ObjectProperty,
    ObjectQuantifier,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.inference.entailment import check_entailments
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf

A, B, C, D = (NamedEntity("class", "urn:" + name) for name in ("A", "B", "C", "D"))
INDIVIDUAL, J = (NamedEntity("named_individual", "urn:" + name) for name in ("i", "j"))
P = ObjectProperty("urn:p")
_AXIOMS = """
Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
Declaration(NamedIndividual(<urn:i>)) Declaration(NamedIndividual(<urn:j>))
Declaration(ObjectProperty(<urn:p>))
SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:D> <urn:B>)
DisjointClasses(<urn:A> <urn:C>) ClassAssertion(<urn:A> <urn:i>)
SubClassOf(<urn:A> ObjectSomeValuesFrom(<urn:p> <urn:D>))
ObjectPropertyAssertion(<urn:p> <urn:i> <urn:j>)
"""


def _native(axioms: str = _AXIOMS, *, imports: str = "") -> OwlDocument:
    """Capture unchanged original functional syntax for the actual parser."""
    return prepare_owl_document(
        "Ontology(<urn:ontology> " + imports + axioms + ")",
        format="functional",
        document_iri="urn:document",
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use an explicit pinned local Java backend without discovery or downloads."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned local DL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_entailed_not_entailed_and_explicit_negative_are_distinct(
    backend: OwlProfileBackend,
) -> None:
    obligations = (
        ClassAssertion(INDIVIDUAL, B, ()),
        ClassAssertion(INDIVIDUAL, C, ()),
        ClassAssertion(INDIVIDUAL, ClassComplement(C), ()),
        ClassAssertion(INDIVIDUAL, D, ()),
        ClassAssertion(INDIVIDUAL, ClassComplement(D), ()),
    )
    result = check_entailments(backend, _native(), obligations)
    assert result.consistent is True and result.consistency_report.verified
    assert result.report.complete and not result.report.verified
    assert tuple(answer.outcome for answer in result.answers) == (
        "entailed",
        "not_entailed",
        "entailed",
        "not_entailed",
        "not_entailed",
    )
    assert tuple(answer.axiom for answer in result.answers) == obligations
    assert (
        result.reasoner is not None and result.reasoner.artifact_version == "1.4.5.519"
    )
    assert result.reasoner.native_version == "1.4.1.513"
    assert all(answer.raw_entailed is not None for answer in result.answers)
    assert {
        i.location for i in result.report.issues if i.code == "owl.not_entailed"
    } == {"1", "3", "4"}


def test_real_restrictions_boolean_and_inverse_queries_go_beyond_named_edges(
    backend: OwlProfileBackend,
) -> None:
    obligations = (
        SubClassOf(A, ObjectQuantifier("some", P, B), ()),
        ClassAssertion(
            INDIVIDUAL, ClassJunction("intersection", (A, ClassComplement(C))), ()
        ),
        ClassAssertion(
            J, ObjectQuantifier("some", ObjectProperty("urn:p", True), A), ()
        ),
    )
    result = check_entailments(backend, _native(), obligations)
    assert result.report.verified, result.report.to_dict()
    assert all(
        answer.outcome == "entailed" and answer.report.verified
        for answer in result.answers
    )
    universal = check_entailments(
        backend,
        _native(),
        (ClassAssertion(INDIVIDUAL, ObjectQuantifier("all", P, B), ()),),
    )
    assert universal.answers[0].outcome == "not_entailed" and universal.report.complete


def test_inconsistent_or_unsupported_work_never_produces_an_ordinary_proof(
    backend: OwlProfileBackend,
) -> None:
    queries = (
        ClassAssertion(INDIVIDUAL, B, ()),
        ClassAssertion(INDIVIDUAL, ClassComplement(C), ()),
    )
    inconsistent = check_entailments(
        backend, _native(_AXIOMS + "ClassAssertion(<urn:C> <urn:i>)"), queries
    )
    assert inconsistent.consistent is False and not inconsistent.report.verified
    assert all(
        answer.outcome == "unknown" and answer.raw_entailed is None
        for answer in inconsistent.answers
    )
    mixed = check_entailments(
        backend,
        _native(),
        (
            queries[0],
            Declaration(A, ()),
            ClassAssertion(INDIVIDUAL, NamedEntity("class", "urn:missing"), ()),
        ),
    )
    assert mixed.consistent is True and not mixed.report.complete
    assert tuple(answer.outcome for answer in mixed.answers) == (
        "entailed",
        "unknown",
        "unknown",
    )
    assert mixed.answers[0].report.verified
    datatype = check_entailments(
        backend,
        _native(
            _AXIOMS + "Declaration(Datatype(<urn:custom>)) "
            "Declaration(DataProperty(<urn:d>)) EquivalentClasses(<urn:C> "
            "DataSomeValuesFrom(<urn:d> <urn:custom>))"
        ),
        queries,
    )
    assert (
        datatype.consistent is None
        and datatype.native_error == "UnsupportedDatatypeException"
    )
    assert all(answer.outcome == "unknown" for answer in datatype.answers)
    missing = check_entailments(
        backend, _native(imports="Import(<https://example.invalid/missing>)"), queries
    )
    assert missing.consistent is None and all(
        answer.outcome == "unknown" for answer in missing.answers
    )


def test_native_imports_and_rdf_loader_scope_are_qualified_before_queries(
    backend: OwlProfileBackend,
) -> None:
    child = prepare_owl_document(
        "Ontology(<urn:child> Declaration(Class(<urn:A>)) "
        "Declaration(Class(<urn:B>)) SubClassOf(<urn:A> <urn:B>))",
        format="functional",
        document_iri="urn:child",
    )
    root = _native("Declaration(Class(<urn:A>))", imports="Import(<urn:child>)")
    result = check_entailments(
        backend, root, (SubClassOf(A, B, ()),), imports={"urn:child": child}
    )
    assert result.report.verified and len(result.documents) == 2
    graph = Graph()
    a, b, i = URIRef("urn:A"), URIRef("urn:B"), URIRef("urn:i")
    graph.add((a, RDF.type, OWL.Class))
    graph.add((b, RDF.type, OWL.Class))
    graph.add((i, RDF.type, OWL.NamedIndividual))
    graph.add((i, RDF.type, a))
    graph.add((a, RDFS.subClassOf, b))
    rdf = check_entailments(
        backend, prepare_rdf(graph), (ClassAssertion(INDIVIDUAL, B, ()),)
    )
    assert rdf.report.verified and len(rdf.loaders) == 1 and rdf.loaders[0].qualified
    assert len(graph) == 5


def test_cumulative_budget_and_cancelled_execution_retain_no_fabricated_answer(
    backend: OwlProfileBackend,
) -> None:
    queries = (ClassAssertion(INDIVIDUAL, B, ()),)
    cancelled = Event()
    cancelled.set()
    stopped = check_entailments(backend, _native(), queries, cancel=cancelled)
    assert stopped.report.outcome == "exhausted" and not stopped.report.verified
    for limits in (
        Limits(max_work=1),
        Limits(max_total_bytes=128),
        Limits(max_states=1),
    ):
        result = check_entailments(backend, _native(), queries, budget=Budget(limits))
        assert result.report.outcome == "exhausted" and not result.report.verified
        assert all(answer.outcome == "unknown" for answer in result.answers)
    recovery = check_entailments(backend, _native(), queries)
    assert recovery.report.verified
