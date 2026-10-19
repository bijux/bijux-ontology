"""Owned selected RDF closures support repeated native consumers without rereads."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from bijux_ontology_reasoning.axioms import ClassAssertion, NamedEntity
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.documents.sources.rdf import capture_rdf_closure
from bijux_ontology_reasoning.inference.consistency import check_consistency
from bijux_ontology_reasoning.inference.entailment import check_entailments
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary


def _root() -> Graph:
    """Keep the exact requested import separate from an actual emitted type."""
    graph = Graph()
    graph.add((URIRef("urn:ontology"), RDF.type, OWL.Ontology))
    graph.add((URIRef("urn:ontology"), OWL.imports, URIRef("urn:child")))
    graph.add((URIRef("urn:i"), RDF.type, URIRef("https://example.org/base/Type")))
    return graph


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Use only an explicit qualified local backend, with no runtime discovery."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not (java and bundle and pin):
        pytest.skip("Explicit pinned DL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_closed_single_read_stream_supports_two_real_native_consumers(
    backend: OwlProfileBackend,
) -> None:
    """Once the caller stream closes, actual independent operations reuse bytes."""
    stream = io.BytesIO(
        b"<urn:document> a <http://www.w3.org/2002/07/owl#Ontology> . "
        b"<Type> a <http://www.w3.org/2002/07/owl#Class> ."
    )
    root = prepare_rdf(_root())
    prior = Report("prior", 0, requested=0)
    reports = [prior]
    closure = capture_rdf_closure(
        root,
        {
            "urn:child": RdfSource(
                stream,
                format="turtle",
                document_iri="urn:document",
                effective_url="https://example.org/base/",
            ),
            "urn:irrelevant": RdfSource("not valid RDF", format="turtle"),
        },
        Operation(None, None, 30, None),
        reports=reports,
    )
    stream.close()
    assert closure.source is root and len(closure.originals) == 2
    assert all(type(value.content) is bytes for value in closure.imports.values())
    assert "urn:irrelevant" not in closure.imports
    assert closure.imports["urn:child"] is closure.imports["urn:document"]
    first = check_consistency(backend, root, imports=closure.imports, named_classes=())
    second = check_entailments(
        backend,
        root,
        (
            ClassAssertion(
                NamedEntity("named_individual", "urn:i"),
                NamedEntity("class", "https://example.org/base/Type"),
                (),
            ),
        ),
        imports=closure.imports,
    )
    assert first.report.verified and second.report.verified
    assert len(first.documents) == 2 and len(second.documents) == 2
    assert all(report.verified for report in closure.reports)
    assert reports[0] is prior and tuple(reports[1:]) == closure.reports


def test_named_and_default_graph_alias_base_and_selection_roundtrip(
    backend: OwlProfileBackend,
) -> None:
    """Snapshots retain physical selection and never add an unrelated graph."""
    text = """@base <https://example.org/base/> .
      <urn:selected> {
       <urn:document> a <http://www.w3.org/2002/07/owl#Ontology> .
       <Type> a <http://www.w3.org/2002/07/owl#Class> . }
      <urn:other> { <Leak> a <http://www.w3.org/2002/07/owl#Class> . }
      <urn:document> a <http://www.w3.org/2002/07/owl#Ontology> .
      <Default> a <http://www.w3.org/2002/07/owl#Class> ."""
    for name, expected in (("urn:selected", "Type"), ("@default", "Default")):
        root_graph = _root()
        root_graph.remove((URIRef("urn:i"), RDF.type, None))
        root_graph.add(
            (URIRef("urn:i"), RDF.type, URIRef("https://example.org/base/" + expected))
        )
        root = prepare_rdf(root_graph)
        closure = capture_rdf_closure(
            root,
            {
                "urn:child": RdfSource(
                    text,
                    format="trig",
                    document_iri="urn:document",
                    effective_url="https://example.org/base/",
                    graph_name=name,
                )
            },
            Operation(None, None, 30, None),
        )
        resource = closure.imports["urn:child"]
        assert resource.format == "trig" and resource.graph_name == name
        assert resource.document_iri == "urn:document"
        assert resource.effective_url == "https://example.org/base/"
        restored = prepare_rdf(resource)
        graph = restored.graph()
        assert set(graph.subjects(RDF.type, OWL.Class)) == {
            URIRef("https://example.org/base/" + expected)
        }
        original = closure.originals[1]
        assert original.graph_name == restored.graph_name
        # The existing maintained resolver sees the same selected import corpus.
        loaded = load_vocabulary(root, imports=closure.imports)
        assert len(loaded._source_handles) == 2
        native = check_consistency(
            backend,
            root,
            imports=closure.imports,
            named_classes=(),
        )
        assert native.report.verified and len(native.documents) == 2


def test_original_shared_blank_ownership_is_rejected_before_snapshot_copy() -> None:
    """A fresh serialization cannot hide an invalid shared original document role."""
    graph = _root()
    graph.add((URIRef("urn:A"), RDFS.subClassOf, BNode()))
    root = prepare_rdf(graph)
    shared = next(root.graph().objects(URIRef("urn:A"), RDFS.subClassOf))
    imported = Graph()
    imported.add((shared, RDF.type, OWL.Restriction))
    with pytest.raises(ResourceError, match="anonymous term ownership"):
        capture_rdf_closure(
            root, {"urn:child": imported}, Operation(None, None, 30, None)
        )
    with pytest.raises(ResourceError, match="anonymous term ownership"):
        capture_rdf_closure(
            root,
            {"urn:child": Graph()},
            Operation(None, None, 30, None),
            additional=(root,),
        )


def test_capture_obeys_the_shared_finite_allowance() -> None:
    """Factory exhaustion stops before an immutable closure can be published."""
    root = prepare_rdf(_root())
    for limits in (
        Limits(max_work=2),
        Limits(max_states=2),
        Limits(max_total_bytes=64),
    ):
        budget = Budget(limits)
        with pytest.raises(LimitExceeded):
            capture_rdf_closure(
                root, {"urn:child": Graph()}, Operation(None, budget, 30, None)
            )
        for kind, count in budget._counts.items():
            assert count <= getattr(
                limits, "max_total_bytes" if kind == "bytes" else "max_" + kind
            )
