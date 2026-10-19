"""Actual MIREOT boundaries, retention, ordinary consumers and honest failures."""

import os
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.axioms.types import (
    NamedEntity,
    ObjectProperty,
    PropertyDomainRange,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.backend.robot import RobotBackend, prepare_robot_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.extraction.mireot import (
    MireotBoundary,
    MireotImport,
    MireotRetention,
    extract_mireot,
)

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.checking import check_models
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = "https://example.org/"
A, B, UPPER = (NamedEntity("class", EX + name) for name in ("A", "B", "Upper"))
P = NamedEntity("object_property", EX + "p")
BOUNDARIES = (
    MireotBoundary(A, UPPER),
    MireotBoundary(B, UPPER),
    MireotBoundary(P, NamedEntity("object_property", EX + "top")),
)
RETENTION = MireotRetention((P,))
PROOFS = (SubClassOf(A, UPPER, ()), SubClassOf(B, UPPER, ()))
SOURCE = """Ontology(<https://example.org/source> <https://example.org/source/version>
 Annotation(<http://www.w3.org/2000/01/rdf-schema#label> "source ontology")
 Declaration(Class(<https://example.org/Upper>))
 Declaration(Class(<https://example.org/Mid>))
 Declaration(Class(<https://example.org/A>)) Declaration(Class(<https://example.org/B>))
 Declaration(Class(<https://example.org/Unrelated>))
 Declaration(Class(<https://example.org/UnrelatedChild>))
 Declaration(ObjectProperty(<https://example.org/p>))
 Declaration(ObjectProperty(<https://example.org/top>))
 Declaration(AnnotationProperty(<https://example.org/note>))
 SubClassOf(<https://example.org/A> <https://example.org/Mid>)
 SubClassOf(<https://example.org/B> <https://example.org/Mid>)
 SubClassOf(<https://example.org/Mid> <https://example.org/Upper>)
 SubClassOf(<https://example.org/UnrelatedChild> <https://example.org/Unrelated>)
 SubObjectPropertyOf(<https://example.org/p> <https://example.org/top>)
 ObjectPropertyDomain(<https://example.org/p> <https://example.org/Upper>)
 ObjectPropertyRange(<https://example.org/p> <https://example.org/Upper>)
 SubClassOf(<https://example.org/A>
  ObjectSomeValuesFrom(<https://example.org/p> <https://example.org/B>))
 AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#label>
  <https://example.org/A> "A label")
 AnnotationAssertion(<https://example.org/note> <https://example.org/A> "A note")
 AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#isDefinedBy>
  <https://example.org/A> <https://example.org/original-owner>)
)"""


@pytest.fixture
def engines(tmp_path: Path) -> tuple[RobotBackend, OwlProfileBackend]:
    """Require actual explicitly pinned optional native releases."""
    java, bundle, pin, robot_java, robot_jar = (
        os.environ.get(name)
        for name in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
            "BIJUX_ROBOT_JAVA",
            "BIJUX_ROBOT_JAR",
        )
    )
    if not (java and bundle and pin and robot_java and robot_jar):
        pytest.skip("Explicit pinned ROBOT and OWL distributions are required")
    return (
        prepare_robot_backend(
            Path(robot_java),
            Path(robot_jar),
            sha256="53cd275c8e4c0dad1fc2c1c34174876795cd7d6b6926e1444adcbb0cb3c7ab26",
            artifacts=tmp_path / "robot",
            limits=Limits(max_total_bytes=128_000_000),
        ),
        prepare_profile_backend(
            Path(java),
            Path(bundle),
            manifest_sha256=pin,
            artifacts=tmp_path / "owl",
        ),
    )


def original(text: str = SOURCE) -> OwlDocument:
    """Retain exact original bytes and explicit parser/resolution identity."""
    return prepare_owl_document(
        text,
        format="functional",
        document_iri=EX + "source",
    )


def run(
    engines: tuple[RobotBackend, OwlProfileBackend],
    *,
    source: OwlDocument | None = None,
    boundaries: tuple[MireotBoundary, ...] = BOUNDARIES,
    retention: MireotRetention = RETENTION,
    postconditions: tuple[SubClassOf, ...] = PROOFS,
    budget: Budget | None = None,
    cancel: Event | None = None,
) -> MireotImport:
    """Use one explicit finite native extraction contract across controls."""
    return extract_mireot(
        *engines,
        original() if source is None else source,
        boundaries,
        retention,
        module_iri=EX + "module",
        module_version_iri=EX + "module/version",
        postconditions=postconditions,
        budget=budget,
        cancel=cancel,
    )


def test_actual_smaller_import_keeps_boundaries_owners_and_core_model(
    engines: tuple[RobotBackend, OwlProfileBackend],
) -> None:
    """Actual classes and property remain usable with original provenance."""
    source = original()
    result = run(engines, source=source)
    assert result.report.verified, result.report.to_dict()
    assert result.source and result.rdf_content
    assert result.original_inspection and result.inspection and result.rdf_inspection
    assert len(result.inspection.axioms) < len(result.original_inspection.axioms)
    header = result.inspection.documents[0]
    assert header.ontology_iri == EX + "module"
    assert header.version_iri == EX + "module/version" and header.annotations
    assert (
        b"original-owner" in result.source.content
        and b"A note" in result.source.content
    )
    assert b"Unrelated" not in result.source.content
    assert result.before and result.after and result.before.report.verified
    assert result.after.report.verified and result.hierarchy
    assert (
        result.hierarchy.report.verified
        and result.native_identity == "ROBOT version 1.9.11"
    )
    vocabulary = load_vocabulary(
        RdfSource(result.rdf_content, format="turtle", document_iri=EX + "module")
    )
    model = ModelDeclaration(
        "record",
        (A.iri,),
        (
            FieldDeclaration(
                "linked", P.iri, (ValueDeclaration("iri", types=(B.iri,)),)
            ),
        ),
    )
    assert check_models((model,), vocabulary).verified
    assert source.content == SOURCE.encode()


def test_missing_unrelated_and_wrong_role_upper_never_pass_native_exit_zero(
    engines: tuple[RobotBackend, OwlProfileBackend],
) -> None:
    """Source membership and actual ancestry precede native permissive filtering."""
    for upper in (
        NamedEntity("class", EX + "missing"),
        NamedEntity("class", EX + "Unrelated"),
        NamedEntity("data_property", UPPER.iri),
    ):
        result = run(engines, boundaries=(MireotBoundary(A, upper),))
        assert (
            not result.report.verified
            and result.source is None
            and result.rdf_content is None
        )
    assert any(issue.code == "owl.mireot_input_error" for issue in result.report)
    inverse = original(
        SOURCE.replace(
            "SubObjectPropertyOf(<https://example.org/p> <https://example.org/top>)",
            "SubObjectPropertyOf(<https://example.org/p> "
            "ObjectInverseOf(<https://example.org/top>))",
        )
    )
    result = run(engines, source=inverse)
    assert result.source is None and any(
        issue.code == "owl.mireot_boundary" for issue in result.report
    )


def test_missing_property_and_dropped_required_axiom_prevent_complete_import(
    engines: tuple[RobotBackend, OwlProfileBackend],
) -> None:
    """A class hierarchy cannot conceal a missing required property or range."""
    for retention in (
        MireotRetention((P,)),
        MireotRetention(
            required_axioms=(
                PropertyDomainRange("range", ObjectProperty(P.iri), UPPER, ()),
            ),
        ),
    ):
        result = run(engines, boundaries=BOUNDARIES[:2], retention=retention)
        assert not result.report.verified and result.source is None
        assert result.inspection and any(
            issue.code == "owl.mireot_retention_failed" for issue in result.report
        )


def test_explicit_root_annotation_omission_and_failed_original_proof(
    engines: tuple[RobotBackend, OwlProfileBackend],
) -> None:
    """Root annotation policy is explicit; entailments are never manufactured."""
    result = run(engines, retention=MireotRetention((P,), ontology_annotations="omit"))
    assert result.report.verified and result.inspection, result.report.to_dict()
    assert not result.inspection.documents[0].annotations
    failed = run(
        engines,
        postconditions=(SubClassOf(A, NamedEntity("class", EX + "Unrelated"), ()),),
    )
    assert failed.source is None and failed.before
    assert any(issue.code == "owl.not_entailed" for issue in failed.report)
    assert failed.native_identity is None


def test_stopped_or_open_source_never_publishes_partial_output(
    engines: tuple[RobotBackend, OwlProfileBackend],
) -> None:
    """Exhaustion, cancellation and unresolved imports retain original ownership."""
    budget = Budget(Limits(max_total_bytes=4096))
    exhausted = run(engines, budget=budget)
    assert exhausted.report.outcome == "exhausted" and exhausted.source is None
    assert budget.remaining("bytes") >= 0
    cancel = Event()
    cancel.set()
    stopped = run(engines, cancel=cancel)
    assert stopped.report.outcome == "exhausted" and stopped.source is None
    opened = run(
        engines,
        source=original(
            SOURCE.replace(
                "Annotation(<http://www.w3.org/2000/01/rdf-schema#label> "
                '"source ontology")',
                "Import(<https://example.org/not-supplied>)",
            )
        ),
    )
    assert not opened.report.verified and opened.source is None
