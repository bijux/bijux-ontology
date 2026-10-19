"""Actual maintained locality extraction and selected original/module entailments."""

import os
from pathlib import Path

import pytest
from bijux_ontology_reasoning.axioms.types import (
    NamedEntity,
    SubClassOf,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents import OwlDocument, prepare_owl_document
from bijux_ontology_reasoning.locality import LocalityMethod, extract_locality_module
from bijux_ontology_reasoning.locality.types import LocalityModule

SOURCE = """Ontology(<urn:original>
 Annotation(<urn:note> "ontology annotation")
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(Class(<urn:C>)) Declaration(Class(<urn:U>))
 Declaration(Class(<urn:V>)) Declaration(AnnotationProperty(<urn:note>))
 SubClassOf(Annotation(<urn:note> "kept") <urn:A> <urn:B>)
 SubClassOf(<urn:B> <urn:C>) SubClassOf(<urn:U> <urn:V>)
 AnnotationAssertion(<http://www.w3.org/2000/01/rdf-schema#label> <urn:A> "A label")
)"""
SIGNATURE = (NamedEntity("class", "urn:A"), NamedEntity("class", "urn:C"))
QUERY = SubClassOf(NamedEntity("class", "urn:A"), NamedEntity("class", "urn:C"), ())


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require an explicit actual pinned maintained native backend."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("An explicit pinned native backend is required")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def original(text: str = SOURCE, identity: str = "urn:source") -> OwlDocument:
    """Prepare actual native source bytes with an explicit resolution identity."""
    return prepare_owl_document(text, format="functional", document_iri=identity)


def extract(
    backend: OwlProfileBackend,
    source: OwlDocument,
    *,
    method: LocalityMethod = "BOT",
    signature: tuple[NamedEntity, ...] = SIGNATURE,
) -> LocalityModule:
    """Use the same explicit finite preservation choices across small controls."""
    return extract_locality_module(
        backend,
        source,
        signature,
        method=method,
        module_iri="urn:module",
        consumer_entailments=(QUERY,),
        import_policy="flatten",
        annotations="retain",
    )


@pytest.mark.parametrize("method", ("BOT", "TOP", "STAR"))
def test_selected_entailment_annotations_and_unrelated_exclusion(
    backend: OwlProfileBackend,
    method: LocalityMethod,
) -> None:
    """The maintained methods preserve a real proof while excluding unrelated axioms."""
    source = original()
    result = extract(backend, source, method=method)
    assert result.report.verified, result.report.to_dict()
    assert result.source and result.before and result.after
    assert (
        result.before.answers[0].outcome
        == result.after.answers[0].outcome
        == "entailed"
    )
    assert result.source.document_iri == "urn:module" and result.method == method
    assert (
        b"urn:U" not in result.source.content and b"urn:V" not in result.source.content
    )
    assert (
        b"ontology annotation" in result.source.content
        and b"A label" in result.source.content
    )
    assert any(
        isinstance(a.axiom, SubClassOf) and a.axiom.annotations for a in result.axioms
    )
    assert all(a.documents == (0,) for a in result.axioms)
    assert source.content == SOURCE.encode()


def test_imported_axioms_flatten_with_exact_original_structural_owners(
    backend: OwlProfileBackend,
) -> None:
    """Equal annotated axioms retain both owners without physical text claims."""
    root = original(
        "Ontology(<urn:root> Import(<urn:import>) "
        "Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>)) "
        "SubClassOf(<urn:A> <urn:B>))",
        "urn:root",
    )
    imported = original(
        "Ontology(<urn:import> Declaration(Class(<urn:A>)) "
        "Declaration(Class(<urn:B>)) Declaration(Class(<urn:C>)) "
        "SubClassOf(<urn:A> <urn:B>) SubClassOf(<urn:B> <urn:C>))",
        "urn:import",
    )
    result = extract_locality_module(
        backend,
        root,
        SIGNATURE,
        method="BOT",
        module_iri="urn:module",
        consumer_entailments=(QUERY,),
        import_policy="flatten",
        annotations="retain",
        imports={"urn:import": imported},
    )
    assert result.report.verified and result.source, result.report.to_dict()
    assert len(result.originals) == 2 and b"Import(" not in result.source.content
    assert any(
        a.documents == (0, 1) and isinstance(a.axiom, SubClassOf) for a in result.axioms
    )
    assert root.content.endswith(b"SubClassOf(<urn:A> <urn:B>))")
    assert imported.content.endswith(b"SubClassOf(<urn:B> <urn:C>))")


def test_missing_signature_roles_and_unselected_consumer_symbols_fail(
    backend: OwlProfileBackend,
) -> None:
    """Source spelling cannot stand in for a missing distinct native entity role."""
    for entity in (
        NamedEntity("class", "urn:missing"),
        NamedEntity("data_property", "urn:A"),
    ):
        result = extract(backend, original(), signature=(*SIGNATURE, entity))
        assert not result.report.verified and result.source is None
        assert any(i.code == "owl.locality_selection_failed" for i in result.report)
    outside = extract_locality_module(
        backend,
        original(),
        SIGNATURE,
        method="BOT",
        module_iri="urn:module",
        consumer_entailments=(
            SubClassOf(
                NamedEntity("class", "urn:A"), NamedEntity("class", "urn:B"), ()
            ),
        ),
        import_policy="flatten",
        annotations="retain",
    )
    assert not outside.report.verified and outside.source is None
    collision = extract_locality_module(
        backend,
        original(),
        SIGNATURE,
        method="BOT",
        module_iri="urn:original",
        consumer_entailments=(QUERY,),
        import_policy="flatten",
        annotations="retain",
    )
    assert not collision.report.verified and collision.source is None


def test_unsupported_original_and_missing_import_cannot_certify_module(
    backend: OwlProfileBackend,
) -> None:
    """An excluded unsupported source axiom cannot disappear behind locality claims."""
    unsupported = original(
        SOURCE[:-1] + "Declaration(ObjectProperty(<urn:p>)) "
        "HasKey(<urn:U> (<urn:p>) ()))"
    )
    result = extract(backend, unsupported)
    assert not result.report.verified and result.source is None
    assert any(i.code == "owl.locality_source_unrepresented" for i in result.report)
    missing = extract(backend, original(SOURCE[:-1] + "Import(<urn:missing>))"))
    assert not missing.report.verified and missing.source is None
