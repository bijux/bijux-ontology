"""Counterfactual and joint removal proofs through the actual maintained backend."""

import os
from pathlib import Path

import pytest

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology_reasoning.axioms import ClassRelation, NamedEntity, SubClassOf
from bijux_ontology_reasoning.axioms.changes import write_axiom_change
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.editing.reductions import (
    analyze_redundancy,
    prepare_reduction,
)

A, B, C = (NamedEntity("class", "urn:" + name) for name in ("A", "B", "C"))
AB, BC, AC = SubClassOf(A, B, ()), SubClassOf(B, C, ()), SubClassOf(A, C, ())
DECLARATIONS = " ".join(
    "Declaration(Class(<urn:" + name + ">))" for name in ("A", "B", "C")
)


def source(body: str, imports: str = "") -> OwlDocument:
    """Keep actual owned root bytes and resolution identity explicit."""
    return prepare_owl_document(
        "Ontology(<urn:reduction> " + imports + DECLARATIONS + body + ")",
        format="functional",
        document_iri="urn:source",
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use a real explicitly qualified maintained native backend."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned native backend is required")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_counterfactual_proposal_and_joint_transitive_reduction(
    backend: OwlProfileBackend,
    tmp_path: Path,
) -> None:
    """One redundant edge is removed while retained annotations and proofs survive."""
    original = source(
        "SubClassOf(<urn:A> <urn:B>) "
        "SubClassOf(Annotation(<http://www.w3.org/2000/01/rdf-schema#comment> "
        '"kept axiom") <urn:B> <urn:C>) '
        "SubClassOf(<urn:A> <urn:C>) "
        "AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#label> "
        '<urn:A> "preserved")'
    )
    analysis = analyze_redundancy(
        backend,
        original,
        (AC, AB),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert analysis.proposals == (AC,) and analysis.report.complete
    assert tuple(check.redundant for check in analysis.checks) == (True, False)
    assert analysis.report.outcome == "disagreement"
    candidate = prepare_reduction(
        backend,
        original,
        analysis.proposals,
        consumer_entailments=(AC,),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert candidate.report.verified and candidate.source
    assert candidate.before and candidate.before[0].verified
    assert candidate.removals == (AC,) and b"preserved" in candidate.source.content
    assert b"kept axiom" in candidate.source.content
    destination = tmp_path / "reduced.ofn"
    assert write_axiom_change(candidate, destination, accept=True).verified
    assert destination.read_bytes() == candidate.source.content
    assert b"SubClassOf(<urn:A> <urn:C>)" in original.content


def test_individually_redundant_assertions_do_not_authorize_their_union(
    backend: OwlProfileBackend,
    tmp_path: Path,
) -> None:
    """Equivalent-class support and its forward edge cannot both be removed."""
    original = source(
        "EquivalentClasses(<urn:A> <urn:B>) "
        "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:B> <urn:A>)"
    )
    equivalent = ClassRelation("equivalent", (A, B), ())
    analysis = analyze_redundancy(
        backend,
        original,
        (AB, equivalent),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert analysis.report.verified and analysis.proposals == (AB, equivalent)
    unsafe = prepare_reduction(
        backend,
        original,
        analysis.proposals,
        consumer_entailments=(AB,),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert not unsafe.report.verified and unsafe.source is not None
    assert any(issue.code == "owl.not_entailed" for issue in unsafe.report)
    destination = tmp_path / "protected.ofn"
    destination.write_bytes(b"original destination")
    assert not write_axiom_change(
        unsafe, destination, accept=True, overwrite=True
    ).verified
    assert destination.read_bytes() == b"original destination"


def test_inconsistent_missing_and_import_only_assertions_cannot_prove_redundancy(
    backend: OwlProfileBackend,
) -> None:
    """Vacuity, unavailable imports and nonowned removals cannot become proposals."""
    bad = source(
        "ClassAssertion(<http://www.w3.org/2002/07/owl#Nothing> <urn:i>) "
        "SubClassOf(<urn:A> <urn:B>)"
    )
    failed = analyze_redundancy(
        backend, bad, (AB,), annotation_policy="retain", import_policy="read_only"
    )
    assert not failed.proposals and failed.checks[0].redundant is None
    assert any(issue.code == "owl.inconsistent" for issue in failed.report)
    missing = analyze_redundancy(
        backend,
        source("", "Import(<urn:import>) "),
        (AB,),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert not missing.proposals and not missing.report.complete
    imported = prepare_owl_document(
        "Ontology(<urn:import> " + DECLARATIONS + "SubClassOf(<urn:A> <urn:B>))",
        format="functional",
        document_iri="urn:import",
    )
    unowned = analyze_redundancy(
        backend,
        source("", "Import(<urn:import>) "),
        (AB,),
        imports={"urn:import": imported},
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert not unowned.proposals and unowned.checks[0].redundant is None
    assert any(
        issue.code == "owl.axiom_precondition_failed" for issue in unowned.report
    )


def test_required_scope_and_bounded_stops_preserve_raw_findings(
    backend: OwlProfileBackend,
) -> None:
    """Incomplete work and unsupported policy cannot certify a candidate."""
    original = source("SubClassOf(<urn:A> <urn:B>)")
    tiny = analyze_redundancy(
        backend,
        original,
        (AB,),
        budget=Budget(Limits(max_work=1)),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert not tiny.proposals and tiny.report.outcome == "exhausted"
    for consumer, annotations, imports_policy in (
        ((), "retain", "read_only"),
        ((AB,), "drop", "read_only"),
        ((AB,), "retain", "edit"),
    ):
        result = prepare_reduction(
            backend,
            original,
            (AB,),
            consumer_entailments=consumer,
            annotation_policy=annotations,
            import_policy=imports_policy,
        )
        assert not result.report.verified and result.source is None
    duplicate = analyze_redundancy(
        backend,
        original,
        (AB, AB),
        annotation_policy="retain",
        import_policy="read_only",
    )
    assert duplicate.report.outcome == "input_error"
