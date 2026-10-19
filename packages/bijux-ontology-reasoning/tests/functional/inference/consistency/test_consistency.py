"""Real mature DL answers preserve consistency, class scope and native failures."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.inference.consistency import check_consistency
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

A, B, C = (URIRef("urn:diagnosis:" + name) for name in ("A", "B", "C"))
_DECLARATIONS = """Declaration(Class(<urn:diagnosis:A>))
Declaration(Class(<urn:diagnosis:B>)) Declaration(Class(<urn:diagnosis:C>))"""
_UNSAT = (
    _DECLARATIONS
    + """
SubClassOf(<urn:diagnosis:A> <urn:diagnosis:B>)
SubClassOf(<urn:diagnosis:A> <urn:diagnosis:C>)
DisjointClasses(<urn:diagnosis:B> <urn:diagnosis:C>)"""
)


def _native(axioms: str, *, imports: str = "") -> OwlDocument:
    """Capture real original functional syntax for the actual native parser."""
    return prepare_owl_document(
        "Ontology(<urn:diagnosis:ontology> " + imports + axioms + ")",
        format="functional",
        document_iri="urn:diagnosis:document",
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Select an explicit sealed local backend; imports never discover Java."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned local DL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_consistent_but_unsatisfiable_is_distinct_from_inconsistent(
    backend: OwlProfileBackend,
) -> None:
    result = check_consistency(backend, _native(_UNSAT))
    assert result.consistent is True and result.consistency_report.verified
    assert result.report.complete and not result.report.verified
    assert {c.iri: c.satisfiable for c in result.classes} == {
        A: False,
        B: True,
        C: True,
    }
    assert result.reasoner is not None
    assert result.reasoner.artifact_version == "1.4.5.519"
    assert result.reasoner.native_version == "1.4.1.513"
    assert not result.reasoner.ignore_unsupported_datatypes
    assert result.in_dl_profile is True and result.native_error is None
    inconsistent = check_consistency(
        backend,
        _native(_UNSAT + "ClassAssertion(<urn:diagnosis:A> <urn:diagnosis:i>)"),
    )
    assert inconsistent.consistent is False and not inconsistent.classes
    assert inconsistent.consistency_report.complete
    assert inconsistent.consistency_report.outcome == "disagreement"
    assert not inconsistent.report.verified
    good = check_consistency(backend, _native(_DECLARATIONS), named_classes=(C, A))
    assert good.report.verified and good.consistent is True
    assert tuple(c.iri for c in good.classes) == (C, A)
    assert all(c.satisfiable and c.report.verified for c in good.classes)
    missing = check_consistency(
        backend, _native(_DECLARATIONS), named_classes=(URIRef("urn:absent"),)
    )
    assert missing.consistent is None and not missing.report.verified


def test_native_unsupported_datatype_profile_and_missing_import_remain_unknown(
    backend: OwlProfileBackend,
) -> None:
    datatype = check_consistency(
        backend,
        _native(
            _DECLARATIONS
            + "Declaration(Datatype(<urn:custom>)) Declaration(DataProperty(<urn:p>)) "
            "EquivalentClasses(<urn:diagnosis:A> "
            "DataSomeValuesFrom(<urn:p> <urn:custom>))"
        ),
    )
    assert datatype.consistent is None and not datatype.report.verified
    assert datatype.native_error == "UnsupportedDatatypeException"
    unsupported = check_consistency(
        backend,
        _native(
            _DECLARATIONS
            + "Declaration(ObjectProperty(<urn:p>)) TransitiveObjectProperty(<urn:p>) "
            "SubClassOf(<urn:diagnosis:A> ObjectMaxCardinality(1 <urn:p>))"
        ),
    )
    assert unsupported.consistent is None and unsupported.in_dl_profile is False
    assert any(
        i.code == "owl.diagnosis_profile_unsupported" for i in unsupported.report.issues
    )
    missing = check_consistency(
        backend,
        _native(_DECLARATIONS, imports="Import(<https://example.invalid/missing>)"),
    )
    assert missing.consistent is None and not missing.classes
    assert not missing.report.verified


def test_explicit_import_closure_and_actual_rdf_loader_qualification(
    backend: OwlProfileBackend,
) -> None:
    child = prepare_owl_document(
        "Ontology(<urn:child> "
        + _DECLARATIONS
        + "DisjointClasses(<urn:diagnosis:B> <urn:diagnosis:C>))",
        format="functional",
        document_iri="urn:child",
    )
    root = _native(
        _DECLARATIONS + "SubClassOf(<urn:diagnosis:A> <urn:diagnosis:B>) "
        "SubClassOf(<urn:diagnosis:A> <urn:diagnosis:C>)",
        imports="Import(<urn:child>)",
    )
    result = check_consistency(
        backend, root, imports={"urn:child": child}, named_classes=(A,)
    )
    assert result.consistent is True and result.classes[0].satisfiable is False
    assert len(result.documents) == 2 and result.report.complete
    graph = Graph()
    for cls in (A, B, C):
        graph.add((cls, RDF.type, OWL.Class))
    graph.add((A, RDFS.subClassOf, B))
    graph.add((A, RDFS.subClassOf, C))
    graph.add((B, OWL.disjointWith, C))
    rdf = check_consistency(backend, prepare_rdf(graph), named_classes=(A,))
    assert rdf.consistent is True and rdf.classes[0].satisfiable is False
    assert len(rdf.loaders) == 1 and rdf.loaders[0].qualified
    selected = prepare_rdf(RdfSource(graph, document_iri="urn:root"))
    # Caller graph remains ordinary RDF and is never replaced with inferred data.
    assert len(graph) == 6 and selected.graph_fidelity == "complete"


def test_budget_cancellation_and_fresh_recovery_cannot_manufacture_answers(
    backend: OwlProfileBackend,
) -> None:
    source = _native(_UNSAT)
    cancelled = Event()
    cancelled.set()
    stopped = check_consistency(backend, source, cancel=cancelled)
    assert stopped.report.outcome == "exhausted" and stopped.consistent is None
    for limits in (
        Limits(max_work=1),
        Limits(max_total_bytes=128),
        Limits(max_states=1),
    ):
        result = check_consistency(backend, source, budget=Budget(limits))
        assert result.report.outcome == "exhausted" and result.consistent is None
    timed = check_consistency(backend, source, timeout=0.000001)
    assert timed.report.outcome == "exhausted" and timed.consistent is None
    recovery = check_consistency(backend, _native(_DECLARATIONS))
    assert recovery.report.verified and recovery.consistent is True
