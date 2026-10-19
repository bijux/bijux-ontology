"""Finite RDF observations become application rules only after explicit acceptance."""

import importlib.util
from threading import Event
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.paths import MulPath

from bijux_ontology.data.discovery import (
    AcceptedContract,
    ContractCandidate,
    DiscoveryField,
    accept_candidate,
    check_contract,
    discover_contracts,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

EX = Namespace("urn:discovery:")
HAS_SHACL = importlib.util.find_spec("pyshacl") is not None
NATIVE = pytest.mark.skipif(not HAS_SHACL, reason="actual optional SHACL backend")


def _corpus() -> tuple[Graph, tuple[URIRef, ...], tuple[DiscoveryField, ...]]:
    """Use ordinary maintained RDF producer terms, including a missing field."""
    graph = Graph()
    records = (EX.a, EX.b, EX.c, EX.d)
    for record in records:
        graph.add((record, RDF.type, EX.Record))
    for record, values in (
        (EX.a, (Literal("01", datatype=XSD.integer, normalize=False),)),
        (EX.b, (Literal("2.50", datatype=XSD.decimal, normalize=False),)),
        (
            EX.d,
            (Literal("3", datatype=XSD.integer), Literal("4.5", datatype=XSD.decimal)),
        ),
    ):
        measurement = BNode()
        graph.add((record, EX.measurement, measurement))
        graph.add((measurement, RDF.type, EX.Measurement))
        if record == EX.d:
            graph.add((measurement, RDF.type, EX.Calibrated))
        for value in values:
            graph.add((measurement, EX.value, value))
    fields = (
        DiscoveryField("measurement", EX.measurement),
        DiscoveryField("value", EX.measurement / EX.value),
    )
    return graph, records, fields


def _discover(
    graph: Graph | PreparedSource,
    records: tuple[URIRef, ...],
    fields: tuple[DiscoveryField, ...],
) -> ContractCandidate:
    """Select actual records and paths without inferred populations or field names."""
    return discover_contracts(graph, records, fields, shape=EX.Contract)


def _outside(graph: Graph) -> None:
    """Emit a Boolean beyond the observed numeric alternatives."""
    graph.add((EX.held, RDF.type, EX.Record))
    graph.add((EX.held, EX.measurement, EX.newMeasurement))
    graph.add((EX.newMeasurement, RDF.type, EX.Measurement))
    graph.add((EX.newMeasurement, EX.value, Literal(True)))


def test_candidate_observations_remain_usable_when_optional_shacl_is_absent() -> None:
    """Core observation never pretends an unavailable engine validated its corpus."""
    graph, records, fields = _corpus()
    candidate = _discover(graph, records, fields)
    assert len(candidate.records) == 4 and len(candidate.fields) == 2
    value = candidate.fields[1]
    assert (value.minimum, value.maximum) == (0, 2)
    assert {v.datatype for v in value.alternatives} == {
        str(XSD.integer),
        str(XSD.decimal),
    }
    assert [len(r.fields[1].endpoints) for r in candidate.records] == [1, 1, 0, 2]
    if HAS_SHACL:
        assert candidate.report.verified, candidate.report.to_dict()
    else:
        assert candidate.native_report is not None and candidate.native_report.verified
        assert not candidate.report.verified and candidate.shape_report is not None
        with pytest.raises(InputError):
            accept_candidate(candidate, accept_observed_bounds=True)


@NATIVE
def test_full_alternatives_and_missing_records_pass_actual_generated_shapes() -> None:
    """All observed type combinations and cardinalities have actual native coverage."""
    graph, records, fields = _corpus()
    candidate = _discover(graph, records, fields)
    assert candidate.report.verified, candidate.report.to_dict()
    assert candidate.native_report is not None and candidate.native_report.verified
    assert candidate.shape_report is not None and candidate.shape_report.verified
    assert candidate.fields[0].alternatives[0].types == (str(EX.Measurement),)
    assert any(
        v.types == (str(EX.Calibrated), str(EX.Measurement))
        for v in candidate.fields[0].alternatives
    )
    assert str(candidate.records[0].fields[1].endpoints[0]) == "01"
    accepted = accept_candidate(candidate, accept_observed_bounds=True)
    assert check_contract(accepted, graph, records).verified
    assert check_contract(accepted, graph, (EX.c,)).verified
    graph.add((EX.a, EX.unselected, Literal("new data")))
    assert candidate.source is not None and len(candidate.source.graph()) < len(graph)


@NATIVE
def test_held_out_values_and_cardinality_do_not_retrain_accepted_policy() -> None:
    """A new datatype fails both checks without expanding accepted alternatives."""
    graph, records, fields = _corpus()
    candidate = _discover(graph, records, fields)
    with pytest.raises(InputError):
        accept_candidate(candidate, accept_observed_bounds=False)
    required = check_contract(
        cast(AcceptedContract, cast(object, candidate)), graph, records
    )
    assert not required.verified and required.outcome == "input_error"
    accepted = accept_candidate(candidate, accept_observed_bounds=True)
    _outside(graph)
    result = check_contract(accepted, graph, (EX.held,))
    assert result.outcome == "disagreement" and any(
        i.code == "emission.value_mismatch" for i in result.issues
    )
    assert len(result.children) == 3 and not result.children[-1].verified
    graph.add((EX.newMeasurement, EX.value, Literal("5", datatype=XSD.integer)))
    graph.add((EX.newMeasurement, EX.value, Literal("6", datatype=XSD.integer)))
    result = check_contract(accepted, graph, (EX.held,))
    assert any(
        i.code == "emission.cardinality_mismatch" or "count" in i.code
        for i in result.issues
    )


@NATIVE
def test_exceptions_empty_and_unsupported_scope_never_promote_to_required() -> None:
    """Invalid and unknown literal outliers retain exact endpoint evidence."""
    graph, records, fields = _corpus()
    node = next(graph.objects(EX.a, EX.measurement))
    invalid = Literal("not-an-integer", datatype=XSD.integer, normalize=False)
    graph.add((node, EX.value, invalid))
    candidate = _discover(graph, records, fields)
    assert not candidate.report.verified
    assert invalid in candidate.records[0].fields[1].endpoints
    assert candidate.fields[1].maximum == 2
    with pytest.raises(InputError):
        accept_candidate(candidate, accept_observed_bounds=True)
    graph.remove((node, EX.value, invalid))
    custom = Literal("opaque", datatype=EX.Custom)
    graph.add((node, EX.value, custom))
    candidate = _discover(graph, records, fields)
    assert (
        not candidate.report.verified
        and custom in candidate.records[0].fields[1].endpoints
    )
    for selection, declarations in (
        ((), fields),
        ((EX.missing,), fields),
        (records, (DiscoveryField("none", EX.absent),)),
        (records, (DiscoveryField("open", MulPath(EX.measurement, "+")),)),
    ):
        candidate = _discover(graph, selection, declarations)
        assert not candidate.report.verified
        with pytest.raises(InputError):
            accept_candidate(candidate, accept_observed_bounds=True)


@NATIVE
def test_physical_graph_and_owned_blank_scope_remain_exact() -> None:
    """Unselected graphs and foreign blank labels cannot contribute corpus evidence."""
    graph, records, fields = _corpus()
    dataset = Dataset()
    for triple in graph:
        dataset.graph(EX.selected).add(triple)
    dataset.graph(EX.other).add((EX.a, EX.measurement, Literal("outlier")))
    source = prepare_rdf(dataset)
    candidate = discover_contracts(
        source, records, fields, shape=EX.Contract, graph_name=EX.selected
    )
    assert candidate.report.verified and candidate.graph == EX.selected
    assert not discover_contracts(
        source, records, fields, shape=EX.Contract
    ).report.verified
    owned = next(
        s
        for s, p, o in source.graph(EX.selected)
        if p == RDF.type and o == EX.Measurement
    )
    assert isinstance(owned, BNode)
    local = discover_contracts(
        source,
        (owned,),
        (DiscoveryField("value", EX.value),),
        shape=EX.Contract,
        graph_name=EX.selected,
    )
    assert local.report.verified, local.report.to_dict()
    foreign = BNode(str(owned))
    assert not discover_contracts(
        source,
        (foreign,),
        (DiscoveryField("value", EX.value),),
        shape=EX.Contract,
        graph_name=EX.selected,
    ).report.verified


def test_bounded_stop_and_cancel_never_publish_accepted_candidates() -> None:
    """Cancellation and finite allowances cannot manufacture agreement."""
    graph, records, fields = _corpus()
    stopped = discover_contracts(
        graph, records, fields, shape=EX.Contract, limits=Limits(max_work=1)
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    event = Event()
    event.set()
    cancelled = discover_contracts(
        graph, records, fields, shape=EX.Contract, cancel=event
    )
    assert cancelled.report.outcome == "exhausted"
    with pytest.raises(InputError):
        accept_candidate(stopped, accept_observed_bounds=True)
    # A completed nested raw record remains visible when later discovery stops.
    simple = Graph()
    population = tuple(EX[str(i)] for i in range(6))
    for record in population:
        simple.add((record, RDF.type, EX.Record))
        simple.add((record, EX.p, Literal("1", datatype=XSD.integer)))
    partial = discover_contracts(
        simple,
        population,
        (DiscoveryField("p", EX.p),),
        shape=EX.Contract,
        limits=Limits(max_work=1900),
    )
    assert partial.report.outcome == "exhausted" and partial.records
    assert all(record.reference.source is partial.source for record in partial.records)
    assert partial.records[0].fields[0].endpoints
    budget = Budget(Limits(max_issues=1))
    graph.add((EX.a, EX.measurement, Literal("bad", datatype=EX.Unknown)))
    result = discover_contracts(
        graph, records, fields, shape=EX.Contract, budget=budget
    )
    assert not result.report.verified and budget.remaining("issues") >= 0
    assert any(i.kind in ("unverified", "exhausted") for i in result.report.issues)
