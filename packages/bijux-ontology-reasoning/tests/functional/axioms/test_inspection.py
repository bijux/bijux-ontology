"""Actual bounded OWLAPI typed projections and honest unsupported selections."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from bijux_ontology_reasoning.axioms import (
    AnnotationAssertion,
    ClassJunction,
    ClassRelation,
    DataCardinality,
    DataEnumeration,
    DataProperty,
    DatatypeDefinition,
    DatatypeRestriction,
    DataValue,
    Declaration,
    IriValue,
    NamedEntity,
    NativeLiteral,
    ObjectCardinality,
    ObjectEnumeration,
    ObjectProperty,
    ObjectQuantifier,
    PropertyDomainRange,
    SubClassOf,
    SubPropertyOf,
    inspect_axioms,
)
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.backend.types import ProfileDocument
from bijux_ontology_reasoning.documents import (
    OwlDocument,
    convert_ontology,
    prepare_owl_document,
)
from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource

_NATIVE = """Ontology(<urn:axioms>
 Annotation(<urn:metadata> <urn:original>)
 Declaration(Class(<urn:A>)) Declaration(Class(<urn:B>))
 Declaration(ObjectProperty(<urn:p>)) Declaration(DataProperty(<urn:d>))
 Declaration(AnnotationProperty(<urn:note>))
 SubClassOf(Annotation(Annotation(<urn:nested> "inner") <urn:note> "attached")
  <urn:A> ObjectIntersectionOf(ObjectSomeValuesFrom(ObjectInverseOf(<urn:p>) <urn:B>)
   ObjectMinCardinality(2 <urn:p> <urn:B>)))
 EquivalentClasses(<urn:B> ObjectOneOf(<urn:i> <urn:j>))
 DatatypeDefinition(<urn:dt> DataOneOf(
  "01"^^<http://www.w3.org/2001/XMLSchema#integer> "hello"@en))
 DataPropertyRange(<urn:d> DatatypeRestriction(<http://www.w3.org/2001/XMLSchema#integer>
  <http://www.w3.org/2001/XMLSchema#minInclusive> "0"^^<http://www.w3.org/2001/XMLSchema#integer>))
 AnnotationAssertion(<urn:note> <urn:A> "name"@en)
)"""


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use an explicit pinned local build; imports never discover a runtime."""
    java, directory, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and directory and pin):
        pytest.skip("Explicit pinned OWL backend and Java are not configured")
    return prepare_profile_backend(
        Path(java), Path(directory), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def _native(value: str = _NATIVE) -> OwlDocument:
    """Capture original functional syntax without an RDF serialization detour."""
    return prepare_owl_document(value, format="functional", document_iri="urn:source")


def test_native_restrictions_roles_literals_annotations(
    backend: OwlProfileBackend,
) -> None:
    """Actual maintained objects retain fillers, qualified counts and annotations."""
    result = inspect_axioms(backend, _native())
    assert result.report.verified and result.report.complete
    assert len(result.documents) == 1 and not result.unsupported
    document = result.documents[0]
    assert isinstance(document.source, OwlDocument)
    assert document.source.content == _NATIVE.encode()
    assert document.ontology_iri == "urn:axioms"
    assert document.rdf_metadata is None
    assert document.annotations[0].value == IriValue("urn:original")
    axioms = [record.axiom for record in result.axioms]
    sub = next(item for item in axioms if isinstance(item, SubClassOf))
    assert isinstance(sub.super_class, ClassJunction)
    existential = next(
        item for item in sub.super_class.operands if isinstance(item, ObjectQuantifier)
    )
    assert existential.property == ObjectProperty("urn:p", inverse=True)
    assert isinstance(existential.filler, NamedEntity)
    assert existential.filler.iri == "urn:B"
    cardinality = next(
        item for item in sub.super_class.operands if isinstance(item, ObjectCardinality)
    )
    assert cardinality.count == 2 and cardinality.qualified
    assert cardinality.property == ObjectProperty("urn:p")
    assert sub.annotations[0].annotations[0].property == "urn:nested"
    assert isinstance(sub.annotations[0].annotations[0].value, NativeLiteral)
    assert sub.annotations[0].annotations[0].value.lexical == "inner"
    relation = next(item for item in axioms if isinstance(item, ClassRelation))
    enumeration = next(
        item for item in relation.classes if isinstance(item, ObjectEnumeration)
    )
    assert {item.iri for item in enumeration.values} == {"urn:i", "urn:j"}
    definition = next(item for item in axioms if isinstance(item, DatatypeDefinition))
    assert isinstance(definition.definition, DataEnumeration)
    assert {item.lexical for item in definition.definition.values} == {"01", "hello"}
    assert (
        next(item for item in definition.definition.values if item.language).language
        == "en"
    )
    ranged = next(item for item in axioms if isinstance(item, PropertyDomainRange))
    assert isinstance(ranged.property, DataProperty)
    assert isinstance(ranged.expression, DatatypeRestriction)
    assert ranged.expression.facets[0].value.lexical == "0"
    assert any(isinstance(item, AnnotationAssertion) for item in axioms)
    with pytest.raises(FrozenInstanceError):
        document.axiom_count = 0  # type: ignore[misc]
    portable = _native(
        "Ontology(<urn:portable> Declaration(Class(<urn:C>)) "
        "Declaration(Class(<urn:D>)) SubClassOf(<urn:C> <urn:D>))"
    )
    original_portable = inspect_axioms(backend, portable)
    converted = convert_ontology(backend, portable, format="owlxml")
    assert converted.report.verified and converted.content
    second = inspect_axioms(
        backend,
        prepare_owl_document(
            converted.content, format="owlxml", document_iri="urn:converted"
        ),
    )
    assert second.report.verified
    assert {item.axiom for item in second.axioms} == {
        item.axiom for item in original_portable.axioms
    }


def test_rdf_blank_renaming_and_selected_physical_scope(
    backend: OwlProfileBackend,
) -> None:
    """RDF restriction blank labels disappear through actual structural parsing."""

    def graph(label: str) -> Graph:
        """Construct the same restriction using distinct original blank labels."""
        result = Graph()
        result.add((URIRef("urn:ontology"), RDF.type, OWL.Ontology))
        result.add((URIRef("urn:C"), RDF.type, OWL.Class))
        result.add((URIRef("urn:D"), RDF.type, OWL.Class))
        result.add((URIRef("urn:p"), RDF.type, OWL.ObjectProperty))
        result.add((URIRef("urn:C"), RDFS.subClassOf, BNode(label)))
        result.add((BNode(label), RDF.type, OWL.Restriction))
        result.add((BNode(label), OWL.onProperty, URIRef("urn:p")))
        result.add((BNode(label), OWL.someValuesFrom, URIRef("urn:D")))
        return result

    first = graph("old")
    source = prepare_rdf(RdfSource(first, document_iri="urn:rdf"))
    first.remove((None, None, None))
    one = inspect_axioms(backend, source)
    two = inspect_axioms(
        backend, prepare_rdf(RdfSource(graph("renamed"), document_iri="urn:second"))
    )
    assert one.report.verified and two.report.verified
    assert {item.axiom for item in one.axioms} == {item.axiom for item in two.axioms}
    assert one.documents[0].rdf_metadata is not None
    assert one.documents[0].rdf_metadata.present
    assert one.documents[0].rdf_metadata.unparsed == 0
    dataset = Dataset()
    for triple in graph("physical"):
        dataset.graph(URIRef("urn:selected")).add(triple)
    dataset.graph(URIRef("urn:hidden")).add(
        (URIRef("urn:hiddenClass"), RDF.type, OWL.Class)
    )
    selected = inspect_axioms(
        backend, prepare_rdf(RdfSource(dataset, graph_name="urn:selected"))
    )
    assert selected.report.verified
    assert isinstance(selected.documents[0].source, ProfileDocument)
    assert selected.documents[0].source.graph_name == URIRef("urn:selected")
    assert not any(
        isinstance(item.axiom, Declaration)
        and item.axiom.entity.iri == "urn:hiddenClass"
        for item in selected.axioms
    )
    union = inspect_axioms(backend, prepare_rdf(RdfSource(dataset, union=True)))
    assert not union.report.verified


def test_unsupported_inventory_and_explicit_exclusions(
    backend: OwlProfileBackend,
) -> None:
    """Unrepresented actual axioms cannot disappear from an accepted all projection."""
    source = _native(
        "Ontology(<urn:o> Declaration(ObjectProperty(<urn:p>)) "
        "TransitiveObjectProperty(<urn:p>))"
    )
    all_axioms = inspect_axioms(backend, source)
    assert not all_axioms.report.verified and not all_axioms.report.complete
    assert [item.kind for item in all_axioms.unsupported] == [
        "TransitiveObjectProperty"
    ]
    assert len(all_axioms.axioms) == 1
    selected = inspect_axioms(backend, source, axiom_types=("Declaration",))
    assert selected.report.verified
    assert len(selected.axioms) == 1 and not selected.unsupported
    assert [(item.kind, item.count) for item in selected.exclusions] == [
        ("TransitiveObjectProperty", 1)
    ]
    absent = inspect_axioms(backend, source, axiom_types=("SubClassOf",))
    assert absent.report.verified and not absent.axioms
    assert sum(item.count for item in absent.exclusions) == 2
    assert not inspect_axioms(
        backend, source, axiom_types=("Invented",)
    ).report.verified
    malformed = prepare_rdf(
        RdfSource(
            "@prefix owl: <http://www.w3.org/2002/07/owl#> . "
            '_:a a owl:AllDisjointClasses; owl:members "not a list" .',
            format="turtle",
        )
    )
    assert not inspect_axioms(backend, malformed).report.verified


def test_imports_and_object_data_property_roles(backend: OwlProfileBackend) -> None:
    """Original effective aliases retain document attribution and typed properties."""
    root = _native(
        "Ontology(<urn:root> Import(<urn:alias>) Declaration(Class(<urn:C>)) "
        "SubClassOf(<urn:C> DataExactCardinality(1 <urn:d> "
        "<http://www.w3.org/2001/XMLSchema#integer>)) "
        'SubClassOf(<urn:C> DataHasValue(<urn:d> "1"^^'
        "<http://www.w3.org/2001/XMLSchema#integer>)) "
        "SubDataPropertyOf(<urn:d> <urn:e>))"
    )
    child = prepare_owl_document(
        "Ontology(<urn:imported> Declaration(DataProperty(<urn:d>)) "
        "Declaration(DataProperty(<urn:e>)))",
        format="functional",
        document_iri="urn:child",
        effective_url="https://example.invalid/imported",
    )
    assert not inspect_axioms(backend, root).report.verified
    result = inspect_axioms(backend, root, imports={"urn:alias": child})
    assert result.report.verified and len(result.documents) == 2
    by_iri = {item.ontology_iri: item for item in result.documents}
    assert by_iri["urn:imported"].source is child
    assert by_iri["urn:root"].imports == ("urn:alias",)
    supers = [
        item.axiom.super_class
        for item in result.axioms
        if isinstance(item.axiom, SubClassOf)
    ]
    assert any(
        isinstance(item, DataCardinality)
        and item.qualified
        and item.property == DataProperty("urn:d")
        for item in supers
    )
    assert any(isinstance(item, DataValue) for item in supers)
    assert any(
        isinstance(item.axiom, SubPropertyOf)
        and isinstance(item.axiom.sub_property, DataProperty)
        for item in result.axioms
    )


def test_bounds_cancellation_and_optional_import(backend: OwlProfileBackend) -> None:
    """Real native operations exhaust, cancel, recover and share host allowances."""
    environment = dict(os.environ, PATH="")
    imported = subprocess.run(
        [sys.executable, "-I", "-B", "-c", "import bijux_ontology_reasoning.axioms"],
        env=environment,
        capture_output=True,
        timeout=10,
    )
    assert imported.returncode == 0
    source = _native()
    cancellation = threading.Event()
    cancellation.set()
    assert (
        inspect_axioms(backend, source, cancel=cancellation).report.outcome
        == "exhausted"
    )
    assert inspect_axioms(backend, source).report.verified
    tiny = inspect_axioms(backend, source, limits=Limits(max_work=1))
    assert tiny.report.outcome == "exhausted" and not tiny.report.complete
    giant = inspect_axioms(
        backend, source, axiom_types=("x" * 100_000,), limits=Limits(max_bytes=1000)
    )
    assert giant.report.outcome == "exhausted" and giant.selected_types is None
    depth = inspect_axioms(backend, source, limits=Limits(max_depth=8))
    assert not depth.report.verified
    large = _native(
        "Ontology(<urn:large> "
        + "".join(f"Declaration(Class(<urn:C{i}>))" for i in range(2000))
        + ")"
    )
    native_limited = inspect_axioms(backend, large, limits=Limits(max_states=600))
    assert native_limited.report.outcome == "exhausted"
    assert not native_limited.report.complete
    budget = Budget(Limits(max_work=200_000))
    first = inspect_axioms(backend, source, budget=budget)
    assert first.report.verified
    remaining = budget.remaining("work")
    assert remaining < budget.limits.max_work
    budget.consume("work", remaining)
    assert inspect_axioms(backend, source, budget=budget).report.outcome == "exhausted"
