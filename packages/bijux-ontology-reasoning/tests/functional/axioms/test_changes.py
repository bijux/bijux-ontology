"""Actual maintained construction, structural transactions and atomic acceptance."""

import os
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms import (
    ClassJunction,
    DataQuantifier,
    DatatypeFacet,
    DatatypeRestriction,
    Declaration,
    IriValue,
    NamedEntity,
    NativeLiteral,
    ObjectProperty,
    ObjectQuantifier,
    OwlAnnotation,
    SubClassOf,
    inspect_axioms,
)
from bijux_ontology_reasoning.axioms.changes import (
    AxiomCheck,
    axiom_base,
    compare_axioms,
    prepare_axiom_change,
    write_axiom_change,
)
from bijux_ontology_reasoning.axioms.types import DataProperty
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import (
    OwlDocument,
    convert_ontology,
    prepare_owl_document,
)
from bijux_ontology_reasoning.inference.consistency import check_consistency
from bijux_ontology_reasoning.inference.entailment import check_entailments

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, Limits

_BASE = """Ontology(<urn:changes>
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:D>))
 Declaration(ObjectProperty(<urn:p>)) Declaration(DataProperty(<urn:d>))
 Declaration(AnnotationProperty(<urn:note>))
 Declaration(AnnotationProperty(<urn:inner>))
 AnnotationAssertion(<urn:note> <urn:C> "literal urn:C")
)"""


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use an explicit pinned native build; package imports discover no runtime."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("An explicit pinned OWL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def source(content: str = _BASE) -> OwlDocument:
    """Own original native bytes with a fixed effective parser identity."""
    return prepare_owl_document(content, format="functional", document_iri="urn:source")


def restriction() -> SubClassOf:
    """Construct annotated object/data restrictions using the existing model."""
    annotation = OwlAnnotation(
        "urn:note",
        IriValue("urn:C"),
        (
            OwlAnnotation(
                "urn:inner",
                NativeLiteral(
                    "literal urn:C", "http://www.w3.org/2001/XMLSchema#string", None
                ),
                (),
            ),
        ),
    )
    data = DatatypeRestriction(
        NamedEntity("datatype", "http://www.w3.org/2001/XMLSchema#integer"),
        (
            DatatypeFacet(
                "http://www.w3.org/2001/XMLSchema#minInclusive",
                NativeLiteral("01", "http://www.w3.org/2001/XMLSchema#integer", None),
            ),
        ),
    )
    return SubClassOf(
        NamedEntity("class", "urn:C"),
        ClassJunction(
            "intersection",
            (
                ObjectQuantifier(
                    "some", ObjectProperty("urn:p"), NamedEntity("class", "urn:D")
                ),
                DataQuantifier("some", DataProperty("urn:d"), data),
            ),
        ),
        (annotation,),
    )


def inspection(backend: OwlProfileBackend) -> AxiomCheck:
    """Require an actual typed projection rather than an empty callback report."""

    def check(document: OwlDocument, budget: Budget) -> Report:
        """Run the ordinary maintained inspection under the same allowance."""
        return inspect_axioms(backend, document, budget=budget).report

    return AxiomCheck("typed-structure", check)


def test_construct_annotated_object_data_intersection(
    backend: OwlProfileBackend,
) -> None:
    """Factory construction preserves annotations and literal lexical data."""
    original = source()
    candidate = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        additions=(restriction(),),
        profiles=("owl2-dl",),
        after_checks=(inspection(backend),),
    )
    assert candidate.report.verified and candidate.source is not None
    assert candidate.profiles[0].in_profile
    inspected = inspect_axioms(backend, candidate.source)
    axiom = next(
        record.axiom
        for record in inspected.axioms
        if isinstance(record.axiom, SubClassOf)
    )
    assert isinstance(axiom.super_class, ClassJunction)
    assert axiom.annotations == restriction().annotations
    assert b'"01"' in candidate.source.content
    assert original.content == _BASE.encode()
    assert b"ObjectIntersectionOf" not in original.content
    converted = convert_ontology(backend, candidate.source, format="owlxml")
    assert converted.report.verified and converted.content is not None
    xml = prepare_owl_document(
        converted.content, format="owlxml", document_iri="urn:xml"
    )
    restored = compare_axioms(backend, candidate.source, xml)
    assert restored.report.verified and not restored.additions and not restored.removals


def test_structural_sets_and_annotated_removal(backend: OwlProfileBackend) -> None:
    """Reordered native set constructors yield no edits; annotations remain identity."""
    left = source(
        "Ontology(<urn:s> Declaration(Class(<urn:C>)) "
        "Declaration(Class(<urn:D>)) Declaration(Class(<urn:E>)) "
        "SubClassOf(<urn:C> ObjectIntersectionOf(<urn:D> <urn:E>)))"
    )
    right = source(
        left.content.decode().replace(
            "ObjectIntersectionOf(<urn:D> <urn:E>)",
            "ObjectIntersectionOf(<urn:E> <urn:D>)",
        )
    )
    difference = compare_axioms(backend, left, right)
    assert (
        difference.report.verified
        and not difference.additions
        and not difference.removals
    )
    original = source()
    built = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        additions=(restriction(),),
        after_checks=(inspection(backend),),
    )
    assert built.source is not None
    delta = compare_axioms(backend, original, built.source)
    assert delta.report.verified and len(delta.additions) == 1 and not delta.removals
    restored = prepare_axiom_change(
        backend,
        built.source,
        expected=axiom_base(built.source),
        removals=delta.additions,
        after_checks=(inspection(backend),),
    )
    assert restored.source is not None and restored.report.verified
    final = compare_axioms(backend, original, restored.source)
    assert final.report.verified and not final.additions and not final.removals


def test_source_preconditions_and_read_only_imports(
    backend: OwlProfileBackend, tmp_path: Path
) -> None:
    """Wrong bases and missing root removals never modify original or imports."""
    original = source()
    wrong = prepare_axiom_change(
        backend,
        original,
        expected=replace(axiom_base(original), sha256="0" * 64),
        additions=(restriction(),),
        after_checks=(inspection(backend),),
    )
    assert wrong.source is None and any(
        issue.code == "owl.axiom_base_mismatch" for issue in wrong.report.issues
    )
    missing = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        removals=(restriction(),),
        after_checks=(inspection(backend),),
    )
    assert missing.source is None and not missing.report.verified
    imported = source("Ontology(<urn:imported> Declaration(Class(<urn:External>)))")
    root = source(
        "Ontology(<urn:root> Import(<urn:imported>) Declaration(Class(<urn:C>)))"
    )
    removal = Declaration(NamedEntity("class", "urn:External"), ())
    refused = prepare_axiom_change(
        backend,
        root,
        expected=axiom_base(root),
        imports={"urn:imported": imported},
        removals=(removal,),
        after_checks=(inspection(backend),),
    )
    assert refused.source is None and not refused.report.verified
    assert b"urn:External" in imported.content
    illegal = SubClassOf(
        NamedEntity("named_individual", "urn:C"), NamedEntity("class", "urn:D"), ()
    )
    invalid_role = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        additions=(illegal,),
        after_checks=(inspection(backend),),
    )
    assert invalid_role.source is None and not invalid_role.report.verified
    xml = (
        '<Ontology xmlns="http://www.w3.org/2002/07/owl#" ontologyIRI="urn:context">'
        '<Declaration><Class IRI="#C"/></Declaration></Ontology>'
    )
    old_base = prepare_owl_document(
        xml,
        format="owlxml",
        document_iri="urn:context-document",
        effective_url="https://example.invalid/old",
    )
    changed_base = prepare_owl_document(
        xml,
        format="owlxml",
        document_iri="urn:context-document",
        effective_url="https://example.invalid/changed",
    )
    old_view, new_view = (
        inspect_axioms(backend, old_base),
        inspect_axioms(backend, changed_base),
    )
    assert old_view.report.verified and new_view.report.verified
    assert {record.axiom for record in old_view.axioms} != {
        record.axiom for record in new_view.axioms
    }
    assert old_base.sha256 == changed_base.sha256
    stale = prepare_axiom_change(
        backend,
        changed_base,
        expected=axiom_base(old_base),
        additions=(Declaration(NamedEntity("class", "urn:Added"), ()),),
        after_checks=(inspection(backend),),
    )
    assert stale.source is None and any(
        issue.code == "owl.axiom_base_mismatch" for issue in stale.report.issues
    )
    destination = tmp_path / "context.ofn"
    destination.write_bytes(b"original-destination")
    assert not write_axiom_change(
        stale, destination, accept=True, overwrite=True
    ).verified
    assert destination.read_bytes() == b"original-destination"


def test_actual_entailment_and_consistency_are_selected_separately(
    backend: OwlProfileBackend,
) -> None:
    """Maintained logical postconditions are independent of passing profile syntax."""
    original = source()
    question = SubClassOf(
        NamedEntity("class", "urn:C"), NamedEntity("class", "urn:D"), ()
    )

    def logical(document: OwlDocument, budget: Budget) -> Report:
        """Ask actual maintained consistency and entailment in one ordinary API."""
        return check_entailments(backend, document, (question,), budget=budget).report

    candidate = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        additions=(question,),
        profiles=("owl2-dl",),
        after_checks=(AxiomCheck("required-subclass", logical),),
    )
    assert candidate.report.verified
    assert candidate.source is not None
    assert check_consistency(backend, candidate.source).consistent is True
    failed = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        after_checks=(AxiomCheck("required-subclass", logical),),
    )
    assert failed.source is not None and not failed.report.verified
    absent = prepare_axiom_change(backend, original, expected=axiom_base(original))
    assert absent.source is not None and not absent.report.verified


def test_atomic_acceptance_failure_and_receipt(
    backend: OwlProfileBackend, tmp_path: Path
) -> None:
    """Only accepted unchanged checked bytes reach the atomic destination."""
    original = source()
    candidate = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        additions=(restriction(),),
        after_checks=(inspection(backend),),
    )
    assert candidate.source is not None and candidate.report.verified
    destination = tmp_path / "ontology.ofn"
    destination.write_bytes(b"old-destination")
    assert not write_axiom_change(candidate, destination, overwrite=True).verified
    assert destination.read_bytes() == b"old-destination"
    event = Event()
    event.set()
    assert (
        write_axiom_change(
            candidate, destination, accept=True, overwrite=True, cancel=event
        ).outcome
        == "exhausted"
    )
    assert destination.read_bytes() == b"old-destination"
    with pytest.raises(ResourceError):
        write_axiom_change(candidate, destination, accept=True)
    assert destination.read_bytes() == b"old-destination"
    substituted = replace(candidate, source=source(candidate.source.content.decode()))
    assert not write_axiom_change(
        substituted, destination, accept=True, overwrite=True
    ).verified
    assert write_axiom_change(
        candidate, destination, accept=True, overwrite=True
    ).verified
    assert destination.read_bytes() == candidate.source.content
    assert not list(tmp_path.glob("*.part"))


def test_unsupported_changed_axiom_and_cumulative_limits(
    backend: OwlProfileBackend,
) -> None:
    """Unsupported differences and genuine cutoffs never become complete changes."""
    first = source("Ontology(<urn:s> Declaration(ObjectProperty(<urn:p>)))")
    second = source(
        "Ontology(<urn:s> Declaration(ObjectProperty(<urn:p>)) "
        "TransitiveObjectProperty(<urn:p>))"
    )
    difference = compare_axioms(backend, first, second)
    assert not difference.report.verified and not difference.report.complete
    assert any(
        issue.code == "owl.axiom_change_unrepresented"
        for issue in difference.report.issues
    )
    original = source()
    stopped = prepare_axiom_change(
        backend, original, expected=axiom_base(original), limits=Limits(max_work=1)
    )
    assert stopped.source is None and stopped.report.outcome == "exhausted"

    def failure(document: OwlDocument, budget: Budget) -> Report:
        """Pay a real application diagnostic before a later check exhausts."""
        del document
        from bijux_ontology.limits import IssueCollector

        issues = IssueCollector(budget)
        issues.append(
            Issue("application.required", "Required application obligation failed")
        )
        return Report("application", 1, tuple(issues), requested=1)

    def stop(document: OwlDocument, budget: Budget) -> Report:
        """Cut the same actual allowance after the first paid postcondition."""
        del document
        budget.consume("work", budget.remaining("work") + 1)
        return Report("unreachable", 1)

    cut = prepare_axiom_change(
        backend,
        original,
        expected=axiom_base(original),
        after_checks=(AxiomCheck("application", failure), AxiomCheck("stop", stop)),
    )
    assert cut.report.outcome == "exhausted"
    assert any(issue.code == "application.required" for issue in cut.report.issues)
    assert original.content == _BASE.encode()
