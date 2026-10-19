"""Actual maintained explanations retain source evidence and independent truth."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from threading import Event
from typing import Literal, cast

import pytest
from bijux_ontology_reasoning.axioms import NamedEntity, SubClassOf
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.diagnosis import check_consistency
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.entailment import check_entailments
from bijux_ontology_reasoning.explanations import ExplanationQuestion, explain_reasoning
from bijux_ontology_reasoning.inference.consistency.types import ConsistencyCheck
from rdflib import URIRef

from bijux_ontology.limits import Budget, Limits

A, B, C = (NamedEntity("class", "urn:" + name) for name in ("A", "B", "C"))
DECLARATIONS = """
Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
Declaration(Class(<urn:C>)) Declaration(NamedIndividual(<urn:i>))
"""
UNSATISFIABLE = (
    DECLARATIONS
    + """
SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:A> <urn:C>)
DisjointClasses(<urn:B> <urn:C>)
"""
)


def _document(body: str, identity: str = "urn:document") -> OwlDocument:
    """Own exact native syntax without inferring a result from the text."""
    return prepare_owl_document(
        "Ontology(<" + identity + "> " + body + ")",
        format="functional",
        document_iri=identity,
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Run an explicit actual local pinned native explanation distribution."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned native explanation backend is not configured")
    return prepare_profile_backend(
        Path(java),
        Path(bundle),
        manifest_sha256=pin,
        artifacts=tmp_path / "jobs",
    )


def test_imported_annotated_support_is_exact_and_independently_reproduces_entailment(
    backend: OwlProfileBackend,
) -> None:
    child = _document(
        DECLARATIONS
        + 'SubClassOf(Annotation(<urn:note> "child evidence") <urn:B> <urn:C>)',
        "urn:child",
    )
    root = _document(
        "Import(<urn:child>) "
        + DECLARATIONS
        + 'SubClassOf(Annotation(<urn:note> "root evidence") <urn:A> <urn:B>)'
    )
    question = ExplanationQuestion(
        "entailment", SubClassOf(A, C, ()), location="declared.field"
    )
    result = explain_reasoning(backend, root, question, imports={"urn:child": child})
    assert result.explanation_report.verified, result.report.to_dict()
    assert result.underlying is not None and result.underlying.report.verified
    proof = result.justification
    assert proof is not None and proof.reproduced and len(proof.axioms) == 2
    assert result.generator_version == "5.0.1" and result.applicable is True
    assert proof.minimality == "neither" and not proof.enumeration_complete
    originals = {
        origin.document.document_iri
        for axiom in proof.axioms
        for origin in axiom.origins
    }
    assert originals == {"urn:document", "urn:child"}
    assert any(
        "root evidence" in origin.rendering
        for axiom in proof.axioms
        for origin in axiom.origins
    )
    assert any(
        "child evidence" in origin.rendering
        for axiom in proof.axioms
        for origin in axiom.origins
    )
    for axiom in proof.axioms:
        assert hashlib.sha256(axiom.rendering.encode()).hexdigest() == axiom.sha256
        assert all(
            hashlib.sha256(origin.rendering.encode()).hexdigest() == origin.sha256
            for origin in axiom.origins
        )
    replay = check_entailments(backend, proof.source, (SubClassOf(A, C, ()),))
    assert replay.report.verified
    removed = check_entailments(
        backend,
        _document(DECLARATIONS + "SubClassOf(<urn:A> <urn:B>)"),
        (SubClassOf(A, C, ()),),
    )
    assert removed.answers[0].outcome == "not_entailed"
    assert root.content.endswith(b")") and b"root evidence" in root.content


def test_unsatisfiability_and_inconsistency_have_real_standalone_support(
    backend: OwlProfileBackend,
) -> None:
    unsat = explain_reasoning(
        backend,
        _document(UNSATISFIABLE),
        ExplanationQuestion("unsatisfiability", class_iri="urn:A"),
    )
    assert unsat.explanation_report.verified, unsat.report.to_dict()
    assert unsat.underlying is not None and unsat.underlying.consistent is True
    assert unsat.report.outcome == "disagreement"
    assert unsat.justification is not None and len(unsat.justification.axioms) == 3
    replay = check_consistency(
        backend,
        unsat.justification.source,
        named_classes=(URIRef("urn:A"),),
    )
    assert replay.consistent is True and replay.classes[0].satisfiable is False
    inconsistent = explain_reasoning(
        backend,
        _document(DECLARATIONS + "ClassAssertion(owl:Nothing <urn:i>)"),
        ExplanationQuestion("inconsistency"),
    )
    assert inconsistent.explanation_report.verified, inconsistent.report.to_dict()
    assert (
        inconsistent.underlying is not None
        and inconsistent.underlying.consistent is False
    )
    assert inconsistent.report.outcome == "disagreement"
    assert (
        inconsistent.justification is not None
        and len(inconsistent.justification.axioms) == 1
    )
    replay_inconsistent = check_consistency(
        backend, inconsistent.justification.source, named_classes=()
    )
    assert replay_inconsistent.consistent is False
    assert any(issue.code == "owl.inconsistent" for issue in inconsistent.report.issues)


def test_false_observations_and_unavailable_tautology_support_keep_distinct_truth(
    backend: OwlProfileBackend,
) -> None:
    source = _document(DECLARATIONS)
    questions = (
        ExplanationQuestion("entailment", SubClassOf(A, B, ())),
        ExplanationQuestion("unsatisfiability", class_iri="urn:A"),
        ExplanationQuestion("inconsistency"),
    )
    for question in questions:
        result = explain_reasoning(backend, source, question)
        assert result.applicable is False and result.justification is None
        assert result.explanation_report.outcome == "not_applicable"
        assert result.explanation_report.exclusions
        assert result.report.checked == result.report.requested == 1
    tautology = explain_reasoning(
        backend, source, ExplanationQuestion("entailment", SubClassOf(A, A, ()))
    )
    assert tautology.underlying is not None and tautology.underlying.report.verified
    assert tautology.justification is None and not tautology.explanation_report.verified
    assert tautology.applicable is True


def test_insufficient_support_and_unknown_native_input_retain_underlying_boundary(
    backend: OwlProfileBackend,
) -> None:
    capped = explain_reasoning(
        backend,
        _document(UNSATISFIABLE),
        ExplanationQuestion("unsatisfiability", class_iri="urn:A"),
        max_axioms=1,
    )
    assert isinstance(capped.underlying, ConsistencyCheck)
    assert capped.underlying.consistent is True
    assert not capped.underlying.classes[0].satisfiable
    assert (
        capped.justification is None
        and capped.explanation_report.outcome == "exhausted"
    )
    assert any(
        issue.code == "owl.unsatisfiable_class" for issue in capped.report.issues
    )
    for source in (
        _document("Import(<https://example.invalid/absent>) " + DECLARATIONS),
        _document(
            DECLARATIONS + "Declaration(Datatype(<urn:custom>)) "
            "Declaration(DataProperty(<urn:d>)) "
            "EquivalentClasses(<urn:A> DataSomeValuesFrom(<urn:d> <urn:custom>))"
        ),
    ):
        unknown = explain_reasoning(
            backend, source, ExplanationQuestion("unsatisfiability", class_iri="urn:A")
        )
        assert unknown.applicable is None and unknown.justification is None
        assert not unknown.explanation_report.verified
    malformed = explain_reasoning(
        backend,
        _document(DECLARATIONS),
        ExplanationQuestion(cast(Literal["entailment"], "invalid")),
    )
    assert malformed.report.outcome == "input_error"
    assert malformed.report.checked == 0 and malformed.report.requested == 2


def test_cumulative_limits_and_cancellation_do_not_fabricate_support(
    backend: OwlProfileBackend,
) -> None:
    source = _document(UNSATISFIABLE)
    question = ExplanationQuestion("unsatisfiability", class_iri="urn:A")
    event = Event()
    event.set()
    stopped = explain_reasoning(backend, source, question, cancel=event)
    assert stopped.report.outcome == "exhausted" and stopped.justification is None
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=128),
    ):
        bounded = explain_reasoning(backend, source, question, budget=Budget(limits))
        assert bounded.report.outcome == "exhausted" and bounded.justification is None
    assert explain_reasoning(backend, source, question).explanation_report.verified
