"""Native support-led counterfactuals with protected consumer and source truth."""

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology_reasoning.axioms import NamedEntity, SubClassOf
from bijux_ontology_reasoning.axioms.changes import write_axiom_change
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.editing.repairs import AllowedReplacement, propose_repairs
from bijux_ontology_reasoning.inference.explanations import (
    ExplanationQuestion,
    SourceAxiom,
    explain_reasoning,
)

A, B, C = (NamedEntity("class", "urn:" + name) for name in ("A", "B", "C"))
AB = SubClassOf(A, B, ())
DECLARATIONS = (
    "Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>)) "
    "Declaration(Class(<urn:C>)) Declaration(NamedIndividual(<urn:i>)) "
    "Declaration(AnnotationProperty(<urn:note>)) "
)
BODY = (
    'SubClassOf(Annotation(<urn:note> "asserted evidence") <urn:A> <urn:B>) '
    "SubClassOf(<urn:A> <urn:C>) DisjointClasses(<urn:B> <urn:C>) "
    "ClassAssertion(<urn:A> <urn:i>)"
)


def source(body: str = BODY, header: str = "") -> OwlDocument:
    """Use declared annotation vocabulary and original native resolution identity."""
    return prepare_owl_document(
        "Ontology(<urn:repair> " + header + DECLARATIONS + body + ")",
        format="functional",
        document_iri="urn:repair-source",
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Run only the actual explicitly installed pinned maintained backend."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("An explicitly pinned native backend is required")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def origins(
    backend: OwlProfileBackend, original: OwlDocument
) -> tuple[SourceAxiom, ...]:
    """Read real established annotated support without hand-built source identities."""
    observation = explain_reasoning(
        backend, original, ExplanationQuestion("inconsistency")
    )
    assert observation.justification and observation.explanation_report.verified
    return tuple(
        origin for axiom in observation.justification.axioms for origin in axiom.origins
    )


def test_two_repairs_preserve_consumer_and_reject_consistency_only_shortcut(
    backend: OwlProfileBackend, tmp_path: Path
) -> None:
    """Two meanings remain choices; losing a required consumer is a real rejection."""
    original = source()
    before = original.content
    selected = origins(backend, original)
    protected = tuple(
        value for value in selected if value.rendering.startswith("ClassAssertion(")
    )
    result = propose_repairs(
        backend, original, consumer_entailments=(AB,), protected_axioms=protected
    )
    assert result.search_complete and result.report.verified
    assert not result.enumeration_complete and result.minimality == "neither"
    assert len(result.alternatives) == 3 and len(result.proposals) == 2
    assert result.explanation and result.explanation.underlying
    assert result.explanation.underlying.consistent is False
    assert any(issue.code == "owl.inconsistent" for issue in result.explanation.report)
    rejected = next(item for item in result.alternatives if item.accepted is False)
    assert rejected.consistency and rejected.consistency.consistent is True
    assert rejected.entailments and not rejected.entailments.report.verified
    assert any(issue.code == "owl.not_entailed" for issue in rejected.report)
    for proposal in result.proposals:
        assert proposal.consistency and proposal.consistency.report.verified
        assert proposal.entailments and proposal.entailments.report.verified
        assert (
            proposal.candidate.source
            and b"asserted evidence" in proposal.candidate.source.content
        )
        assert not any(edit.origin in protected for edit in proposal.edits)
    destination = tmp_path / "chosen.ofn"
    assert write_axiom_change(
        result.proposals[0].candidate, destination, accept=True
    ).verified
    assert original.content == before
    assert not write_axiom_change(
        rejected.candidate, tmp_path / "rejected.ofn", accept=True
    ).verified


def test_authored_replacement_is_an_independent_explicit_alternative(
    backend: OwlProfileBackend,
) -> None:
    """Keep authored meaning distinct from deletion with original annotations."""
    original = source()
    selected = origins(backend, original)
    changed = next(
        value for value in selected if value.rendering == "SubClassOf(<urn:A> <urn:C>)"
    )
    protected = tuple(value for value in selected if value != changed)
    authored = SubClassOf(
        A, NamedEntity("class", "http://www.w3.org/2002/07/owl#Thing"), ()
    )
    result = propose_repairs(
        backend,
        original,
        consumer_entailments=(AB,),
        protected_axioms=protected,
        allowed_replacements=(
            AllowedReplacement(
                changed, (authored,), "retain explicit broad superclass"
            ),
        ),
    )
    assert result.report.verified and result.search_complete
    assert len(result.proposals) == 2
    assert {item.edits[0].additions for item in result.proposals} == {(), (authored,)}
    assert all(item.candidate.source for item in result.proposals)


def test_import_support_is_read_only_and_empty_edit_scope_is_explicit(
    backend: OwlProfileBackend,
) -> None:
    """A conflicting imported axiom can support diagnosis without being removable."""
    imported = prepare_owl_document(
        "Ontology(<urn:import> " + DECLARATIONS + "DisjointClasses(<urn:B> <urn:C>))",
        format="functional",
        document_iri="urn:import",
    )
    original = source(
        BODY.replace("DisjointClasses(<urn:B> <urn:C>)", ""), "Import(<urn:import>) "
    )
    result = propose_repairs(
        backend, original, imports={"urn:import": imported}, consumer_entailments=(AB,)
    )
    assert (
        result.report.verified
        and result.explanation
        and result.explanation.justification
    )
    assert any(
        origin.document.sha256 == imported.sha256
        for axiom in result.explanation.justification.axioms
        for origin in axiom.origins
    )
    assert all(
        edit.origin.document.sha256 == original.sha256
        for item in result.alternatives
        for edit in item.edits
    )
    all_protected = tuple(
        origin
        for axiom in result.explanation.justification.axioms
        for origin in axiom.origins
    )
    protected = propose_repairs(
        backend,
        original,
        imports={"urn:import": imported},
        consumer_entailments=(AB,),
        protected_axioms=all_protected,
    )
    assert (
        protected.search_complete
        and protected.report.verified
        and not protected.alternatives
    )
    assert imported.content.endswith(b"DisjointClasses(<urn:B> <urn:C>))")


def test_protection_uses_native_structure_and_rejects_forged_source_membership(
    backend: OwlProfileBackend,
) -> None:
    """Protect native membership despite whitespace and reject foreign owners."""
    original = source()
    selected = origins(backend, original)
    changed = next(
        value for value in selected if value.rendering == "SubClassOf(<urn:A> <urn:C>)"
    )
    rendering = "SubClassOf( <urn:A> <urn:C> )"
    alternate_spelling = replace(
        changed,
        rendering=rendering,
        sha256=hashlib.sha256(rendering.encode()).hexdigest(),
    )
    protected = propose_repairs(
        backend,
        original,
        consumer_entailments=(AB,),
        protected_axioms=tuple(value for value in selected if value != changed)
        + (alternate_spelling,),
    )
    assert protected.report.verified and not protected.alternatives
    forged = replace(changed, document=source(""))
    invalid = propose_repairs(
        backend, original, consumer_entailments=(AB,), protected_axioms=(forged,)
    )
    assert not invalid.proposals and invalid.report.outcome == "input_error"


def test_finite_prefix_limits_and_missing_requirements_never_claim_completion(
    backend: OwlProfileBackend,
) -> None:
    """Retain a checked prefix without claiming complete or global enumeration."""
    original = source()
    prefix = propose_repairs(
        backend, original, consumer_entailments=(AB,), max_candidates=1
    )
    assert not prefix.search_complete and len(prefix.alternatives) == 1
    assert prefix.alternatives[0].candidate.source and not prefix.report.verified
    assert any(issue.kind == "exhausted" for issue in prefix.report)
    for kwargs in (
        {"consumer_entailments": ()},
        {"consumer_entailments": (AB,), "max_candidates": 0},
    ):
        invalid = propose_repairs(backend, original, **kwargs)
        assert not invalid.alternatives and invalid.report.outcome == "input_error"
    tiny = propose_repairs(
        backend, original, consumer_entailments=(AB,), budget=Budget(Limits(max_work=1))
    )
    assert not tiny.alternatives and tiny.report.outcome == "exhausted"
    cancelled = Event()
    cancelled.set()
    stopped = propose_repairs(
        backend, original, consumer_entailments=(AB,), cancel=cancelled
    )
    assert not stopped.proposals and not stopped.search_complete
