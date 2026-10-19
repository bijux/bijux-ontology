"""Real emitted assertions use global OWL truth and distinct record conformance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.backend import OwlProfileBackend, prepare_profile_backend
from bijux_ontology_reasoning.inference.instances import check_emitted_consistency
from rdflib import BNode, Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

A, B, C, DEAD, P, INDIVIDUAL, J, K = (
    URIRef("urn:" + name) for name in ("A", "B", "C", "Dead", "p", "i", "j", "k")
)


@dataclass(frozen=True)
class Record:
    """An ordinary RDFLib serializer owns the assertions submitted for diagnosis."""

    subject: URIRef
    types: tuple[URIRef, ...]
    links: tuple[URIRef, ...] = ()

    def emit(self, graph: Graph) -> None:
        """Produce actual RDF terms, without a validation or inference surrogate."""
        for kind in self.types:
            graph.add((self.subject, RDF.type, kind))
        for target in self.links:
            graph.add((self.subject, P, target))


def _ontology() -> PreparedSource:
    """Own real class disjointness, an existential and a functional property."""
    graph = Graph()
    for kind in (A, B, C, DEAD):
        graph.add((kind, RDF.type, OWL.Class))
    graph.add((A, OWL.disjointWith, B))
    graph.add((DEAD, RDFS.subClassOf, A))
    graph.add((DEAD, RDFS.subClassOf, B))
    graph.add((P, RDF.type, OWL.ObjectProperty))
    graph.add((P, RDF.type, OWL.FunctionalProperty))
    restriction = BNode()
    graph.add((C, RDFS.subClassOf, restriction))
    graph.add((restriction, RDF.type, OWL.Restriction))
    graph.add((restriction, OWL.onProperty, P))
    graph.add((restriction, OWL.someValuesFrom, A))
    return prepare_rdf(graph)


def _data(*records: Record) -> PreparedSource:
    """Prepare the caller's complete actual serializer output."""
    graph = Graph()
    for record in records:
        record.emit(graph)
    return prepare_rdf(graph)


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require an explicit installed native distribution without discovery."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_OWL_JAVA",
            "BIJUX_OWL_BUNDLE",
            "BIJUX_OWL_MANIFEST_SHA256",
        )
    )
    if not (java and bundle and pin):
        pytest.skip("Explicit pinned DL backend is not configured")
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "jobs"
    )


def test_actual_emitted_valid_unsatisfiable_and_disjoint_instances(
    backend: OwlProfileBackend,
) -> None:
    ontology = _ontology()
    for types, expected in (((A,), True), ((DEAD,), False), ((A, B), False)):
        emitted = _data(Record(INDIVIDUAL, types))
        binding = RdfModelUsage("Record", INDIVIDUAL, location="records[0]")
        before = set(emitted.graph())
        result = check_emitted_consistency(backend, ontology, emitted, (binding,))
        assert result.diagnosis is not None and result.diagnosis.consistent is expected
        assert result.report.complete and result.report.verified is expected
        assert result.emitted is not None and result.emitted.source is emitted
        assert result.ontology is not None and result.ontology.source is ontology
        assert (
            result.records[0].binding is binding and result.records[0].report.verified
        )
        assert result.records[0].participating_statements == len(types)
        assert set(emitted.graph()) == before
        assert result.derived is not None and result.derived is not emitted


def test_open_world_existentials_and_nonunique_names_are_not_submission_failures(
    backend: OwlProfileBackend,
) -> None:
    ontology = _ontology()
    emitted = _data(Record(INDIVIDUAL, (C,)))
    binding = RdfModelUsage("Record", INDIVIDUAL, location="records[0]")
    logical = check_emitted_consistency(backend, ontology, emitted, (binding,))
    assert logical.report.verified
    model = ModelDeclaration(
        "Record",
        (str(C),),
        (
            FieldDeclaration(
                "value", str(P), (ValueDeclaration("iri"),), emission="guaranteed"
            ),
        ),
    )
    submission = check_rdf_emission(emitted, (model,), (binding,))
    assert not submission.verified
    assert any(
        i.code == "emission.required_entity_fact_missing" for i in submission.issues
    )
    names = _data(Record(INDIVIDUAL, (A,), (J, K)))
    allowed = check_emitted_consistency(backend, ontology, names, (binding,))
    assert allowed.report.verified
    asserted = names.graph()
    asserted.add((J, OWL.differentFrom, K))
    conflict = check_emitted_consistency(
        backend, ontology, prepare_rdf(asserted), (binding,)
    )
    assert conflict.diagnosis is not None and conflict.diagnosis.consistent is False


def test_multirecord_contradiction_retains_only_global_guilt(
    backend: OwlProfileBackend,
) -> None:
    emitted = _data(Record(INDIVIDUAL, (C,), (J, K)), Record(J, (A,)), Record(K, (B,)))
    bindings = tuple(
        RdfModelUsage("Record", subject, location=f"records[{index}]")
        for index, subject in enumerate((INDIVIDUAL, J, K))
    )
    result = check_emitted_consistency(backend, _ontology(), emitted, bindings)
    assert result.diagnosis is not None and result.diagnosis.consistent is False
    assert result.report.complete and not result.report.verified
    assert result.attribution_report.verified and all(
        r.report.verified for r in result.records
    )
    assert tuple(r.binding for r in result.records) == bindings
    findings = [i for i in result.report.issues if i.kind == "finding"]
    assert findings and all(not i.location.startswith("records[") for i in findings)
    assert all(not r.report.issues for r in result.records)


def test_physical_selection_excludes_other_graphs_but_never_unbound_selected_nodes(
    backend: OwlProfileBackend,
) -> None:
    dataset = Dataset()
    dataset.graph(URIRef("urn:selected")).add((INDIVIDUAL, RDF.type, A))
    dataset.graph(URIRef("urn:other")).add((INDIVIDUAL, RDF.type, DEAD))
    emitted = prepare_rdf(RdfSource(dataset, graph_name="urn:selected"))
    binding = RdfModelUsage("Record", INDIVIDUAL)
    result = check_emitted_consistency(backend, _ontology(), emitted, (binding,))
    assert result.report.verified and result.emitted is not None
    assert result.emitted.graph_kind == "named" and result.emitted.graph_name == URIRef(
        "urn:selected"
    )
    dataset.graph(URIRef("urn:selected")).add((J, RDF.type, DEAD))
    unbound = check_emitted_consistency(
        backend,
        _ontology(),
        prepare_rdf(RdfSource(dataset, graph_name="urn:selected")),
        (binding,),
    )
    assert unbound.diagnosis is not None and unbound.diagnosis.consistent is False
    assert (
        len(unbound.records) == 1 and unbound.records[0].binding.subject == INDIVIDUAL
    )
    assert result.report.verified


def test_missing_and_foreign_bindings_and_import_scope_remain_unknown(
    backend: OwlProfileBackend,
) -> None:
    emitted = _data(Record(INDIVIDUAL, (A,)))
    missing = check_emitted_consistency(
        backend, _ontology(), emitted, (RdfModelUsage("Record", J),)
    )
    assert missing.diagnosis is not None and missing.diagnosis.consistent is True
    assert not missing.report.verified and not missing.attribution_report.complete
    graph = Graph()
    graph.add((BNode("record"), RDF.type, A))
    anonymous = prepare_rdf(graph)
    owned = next(anonymous.graph().subjects())
    assert isinstance(owned, BNode)
    foreign = check_emitted_consistency(
        backend, _ontology(), anonymous, (RdfModelUsage("Record", BNode(str(owned))),)
    )
    assert not foreign.report.verified and not foreign.records[0].report.verified
    valid = check_emitted_consistency(
        backend, _ontology(), anonymous, (RdfModelUsage("Record", owned),)
    )
    assert valid.report.verified
    imports = _ontology().graph()
    imports.add((URIRef("urn:ontology"), OWL.imports, URIRef("urn:absent")))
    unavailable = check_emitted_consistency(
        backend, prepare_rdf(imports), emitted, (RdfModelUsage("Record", INDIVIDUAL),)
    )
    assert unavailable.diagnosis is None and not unavailable.report.verified
    empty = check_emitted_consistency(backend, _ontology(), emitted, ())
    assert empty.report.outcome == "input_error" and empty.diagnosis is None


def test_shared_cut_precancellation_and_fresh_native_recovery(
    backend: OwlProfileBackend,
) -> None:
    ontology, emitted = _ontology(), _data(Record(INDIVIDUAL, (DEAD,)))
    binding = (RdfModelUsage("Record", INDIVIDUAL),)
    event = Event()
    event.set()
    cancelled = check_emitted_consistency(
        backend, ontology, emitted, binding, cancel=event
    )
    assert cancelled.report.outcome == "exhausted" and cancelled.diagnosis is None
    budget = Budget(Limits(max_work=2))
    cut = check_emitted_consistency(backend, ontology, emitted, binding, budget=budget)
    assert cut.report.outcome == "exhausted" and budget.remaining("work") >= 0
    recovered = check_emitted_consistency(backend, ontology, emitted, binding)
    assert recovered.diagnosis is not None and recovered.diagnosis.consistent is False
    assert recovered.report.complete and any(
        i.code == "owl.inconsistent" for i in recovered.report.issues
    )
