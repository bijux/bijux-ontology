"""Real public checks remain contradictory after bounded structural reductions."""

import importlib.util
import json
import sys
from dataclasses import replace
from threading import Event
from types import FrameType
from typing import Literal as EventKind

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, SKOS

from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, BudgetKind, Limits
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.jsonld.contracts.codecs import expectations_from_data
from bijux_ontology.jsonld.usages.checking import check_context
from bijux_ontology.rdf.records.types import PatchBlank
from bijux_ontology.workflows.reproductions import (
    ContextRequest,
    EmissionRequest,
    FailureSignature,
    OperationResult,
    ReproductionUnavailable,
    ShaclRequest,
    SkosRequest,
    failure_signature,
    json_resource,
    prepare_context_request,
    prepare_emission_request,
    prepare_shacl_request,
    prepare_skos_request,
    reduce_failure,
    run_reproduction,
)
from bijux_ontology.workflows.reproductions.types import ReproductionRequest

EX = Namespace("urn:reproduction:")
CONTROL_LIMITS = Limits(
    max_work=20_000_000, max_states=2_000_000, max_total_bytes=500_000_000
)
EXPECTATIONS = [
    {"path": ["label"], "predicate": str(EX.label), "value_kind": "literal"}
]


def _request(noise: int = 0) -> ContextRequest:
    context: dict[str, object] = {"label": {"@id": str(EX.label), "@type": "@id"}}
    document: dict[str, object] = {
        "@context": context,
        "@id": str(EX.subject),
        "label": str(EX.value),
    }
    for index in range(noise):
        context[f"extra{index}"] = str(EX[f"extra{index}"])
        document[f"extra{index}"] = "unneeded"
    return prepare_context_request(document, EXPECTATIONS)


def _establish(
    request: ReproductionRequest,
) -> tuple[OperationResult, FailureSignature]:
    result = run_reproduction(request, limits=CONTROL_LIMITS)
    assert result.report.complete and result.report.outcome == "disagreement", (
        result.report.to_dict()
    )
    assert result.report.checked and result.report.requested
    return result, failure_signature(request, result, result.report.issues[0])


def test_context_reduction_replays_same_complete_native_failure() -> None:
    pytest.importorskip("pyld")
    request = _request(24)
    original, signature = _establish(request)
    reduction = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=60)
    assert reduction.request is not None and isinstance(
        reduction.request, ContextRequest
    )
    assert reduction.result is not None and reduction.result.report.complete
    assert (
        reduction.accepted > 0
        and len(reduction.request.document_json) < len(request.document_json) // 2
    )
    assert reduction.result.report.issues == original.report.issues
    actual = check_context(
        reduction.request.document,
        expectations_from_data(json.loads(reduction.request.expectations_json)),
    )
    assert actual.complete and actual.issues == original.report.issues
    document = reduction.request.document
    assert isinstance(document, dict)
    context = document["@context"]
    assert isinstance(context, dict)
    label = context["label"]
    assert isinstance(label, dict) and label["@type"] == "@id"
    changed = {
        "@context": {"label": str(EX.label)},
        "@id": str(EX.subject),
        "label": str(EX.value),
    }
    assert check_context(changed, expectations_from_data(EXPECTATIONS)).verified
    predicate = prepare_context_request(
        {
            "@context": {"@vocab": str(EX), "label": str(EX.wrong)},
            "@id": str(EX.subject),
            "label": "text",
        },
        EXPECTATIONS,
    )
    _, predicate_signature = _establish(predicate)
    selected = reduce_failure(
        predicate, predicate_signature, limits=CONTROL_LIMITS, timeout=60
    )
    assert isinstance(selected.request, ContextRequest)
    selected_document = selected.request.document
    assert isinstance(selected_document, dict)
    selected_context = selected_document["@context"]
    assert isinstance(selected_context, dict)
    assert selected_context["label"] == str(EX.wrong)


def test_named_array_selector_rebinding_preserves_actual_subject_and_value() -> None:
    pytest.importorskip("pyld")
    records = [
        {"@id": str(EX[f"record{i}"]), "label": str(EX[f"value{i}"])} for i in range(4)
    ]
    document = {
        "@context": {
            "records": str(EX.records),
            "label": {"@id": str(EX.label), "@type": "@id"},
        },
        "@id": str(EX.subject),
        "records": records,
    }
    request = prepare_context_request(
        document,
        [
            {
                "path": ["records", 3, "label"],
                "predicate": str(EX.label),
                "value_kind": "literal",
            }
        ],
    )
    _, signature = _establish(request)
    result = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=60)
    assert isinstance(result.request, ContextRequest) and result.result is not None
    assert result.rebindings and result.result.obligations[0].subjects == (
        str(EX.record3),
    )
    assert result.result.obligations[0].raw_json == json.dumps(str(EX.value3))
    assert result.result.obligations[0].path == ("records", 0, "label")
    selected_document = result.request.document
    assert isinstance(selected_document, dict)
    selected_records = selected_document["records"]
    assert isinstance(selected_records, list)
    first = selected_records[0]
    assert isinstance(first, dict) and first["@id"] == str(EX.record3)


def test_emission_reduction_keeps_required_model_and_source_obligations() -> None:
    pytest.importorskip("pyld")
    models = [
        {
            "identifier": "record",
            "types": [],
            "fields": [
                {
                    "name": "label",
                    "predicate": str(EX.label),
                    "values": [{"kind": "literal"}],
                }
            ],
        }
    ]
    models.extend(
        {"identifier": f"unused{i}", "types": [], "fields": []} for i in range(6)
    )
    request = prepare_emission_request(
        _request(8).document, models, [{"model": "record", "path": []}]
    )
    original, signature = _establish(request)
    result = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=90)
    assert result.report.complete, result.report.to_dict()
    assert result.request is not None and result.result is not None
    assert (
        result.result.report.complete
        and result.result.report.issues == original.report.issues
    )
    assert isinstance(result.request, EmissionRequest)
    assert len(json.loads(result.request.models_json)) == 1
    assert json.loads(result.request.usages_json) == [{"model": "record", "path": []}]
    assert result.result.obligations[0].subjects == (str(EX.subject),)


def test_resource_closure_and_false_candidate_failures_cannot_establish_signature() -> (
    None
):
    pytest.importorskip("pyld")
    resource = json_resource(
        str(EX.context), {"@context": {"label": {"@id": str(EX.label), "@type": "@id"}}}
    )
    unused = json_resource(str(EX.unused), {"@context": {"unneeded": str(EX.unneeded)}})
    request = prepare_context_request(
        {"@context": str(EX.context), "@id": str(EX.subject), "label": str(EX.value)},
        EXPECTATIONS,
        resources=(resource, unused),
    )
    _, signature = _establish(request)
    result = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=60)
    assert isinstance(result.request, ContextRequest) and result.result is not None
    assert tuple(value.iri for value in result.request.resources) == (str(EX.context),)
    assert result.result.report.complete
    for document in (
        {"@context": str(EX.missing), "label": "x"},
        {"@context": 42, "label": "x"},
        {"@context": {"label": {"@id": []}}, "label": "x"},
    ):
        candidate = prepare_context_request(document, EXPECTATIONS)
        shared = Budget(CONTROL_LIMITS)
        failed = run_reproduction(candidate, budget=shared)
        assert not failed.report.complete
        if any(
            "qualified worker control receipt" in issue.message
            for issue in failed.report.issues
        ):
            assert all(
                shared.remaining(kind) == 0
                for kind in (
                    "bytes",
                    "resources",
                    "triples",
                    "work",
                    "issues",
                    "states",
                )
            )
        with pytest.raises(ReproductionUnavailable):
            failure_signature(candidate, failed, failed.report.issues[0])
    with pytest.raises(ReproductionUnavailable):
        failure_signature(request, result.result, result.result.report.issues[0])


def test_skos_statement_reduction_retains_real_selected_label_conflict() -> None:
    graph = Graph()
    graph.add((EX.concept, RDF.type, SKOS.Concept))
    graph.add((EX.concept, SKOS.prefLabel, Literal("one", lang="en")))
    graph.add((EX.concept, SKOS.prefLabel, Literal("two", lang="en")))
    for index in range(12):
        graph.add((EX[f"unselected{index}"], EX.note, Literal(str(index))))
    request = prepare_skos_request(
        graph, concepts=(str(EX.concept),), rules=("label-integrity",)
    )
    original, signature = _establish(request)
    result = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=60)
    assert result.request is not None and result.result is not None
    assert isinstance(result.request, SkosRequest)
    assert len(result.request.source.quads) < len(request.source.quads)
    assert result.request.concepts == request.concepts
    assert (
        result.result.report.issues == original.report.issues
        and result.result.report.complete
    )


def test_shacl_list_topology_and_unavailable_anonymous_identity() -> None:
    pytest.importorskip("pyshacl")
    data, shapes = Graph(), Graph()
    data.add((EX.subject, EX.label, Literal("wrong")))
    for index in range(6):
        data.add((EX[f"noise{index}"], EX.note, Literal(str(index))))
    head = BNode()
    shapes.add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.property, EX.Property))
    shapes.add((EX.Property, SH.path, EX.label))
    shapes.add((EX.Property, SH["in"], head))
    shapes.add((head, RDF.first, Literal("allowed")))
    shapes.add((head, RDF.rest, RDF.nil))
    request = prepare_shacl_request(
        data, shapes, focus_nodes=(EX.subject,), use_shapes=(EX.Shape,)
    )
    original, signature = _establish(request)
    reduced = reduce_failure(request, signature, limits=CONTROL_LIMITS, timeout=60)
    assert isinstance(reduced.request, ShaclRequest) and reduced.result is not None
    coupled = tuple(
        quad
        for quad in request.shapes.quads
        if any(
            isinstance(term, PatchBlank)
            for term in (quad.subject, quad.value, quad.graph)
        )
    )
    assert coupled and all(quad in reduced.request.shapes.quads for quad in coupled)
    assert (
        reduced.result.report.complete
        and reduced.result.shacl_findings_json == original.shacl_findings_json
    )
    assert len(reduced.request.data.quads) < len(request.data.quads)
    anonymous = BNode()
    changed = Graph()
    changed.add((EX.Shape, SH.property, anonymous))
    changed.add((anonymous, SH.path, EX.label))
    changed.add((anonymous, SH.nodeKind, SH.IRI))
    unavailable = run_reproduction(
        prepare_shacl_request(
            data, changed, focus_nodes=(EX.subject,), use_shapes=(EX.Shape,)
        ),
        limits=CONTROL_LIMITS,
    )
    assert not unavailable.report.complete
    assert any(issue.kind == "unverified" for issue in unavailable.report.issues)
    assert (
        unavailable.report.children[0].complete
        and unavailable.report.children[0].outcome == "disagreement"
    )


def test_bounded_search_retains_verified_best_and_shared_limits_never_pass() -> None:
    pytest.importorskip("pyld")
    request = _request(12)
    original, signature = _establish(request)
    stopped = reduce_failure(
        request, signature, maximum_trials=1, limits=CONTROL_LIMITS
    )
    assert stopped.request is not None and stopped.result is not None
    assert (
        stopped.result.report.complete
        and stopped.result.report.issues == original.report.issues
    )
    assert stopped.report.outcome == "exhausted" and not stopped.report.complete
    for field in ("max_work", "max_states", "max_total_bytes"):
        result = run_reproduction(request, limits=replace(CONTROL_LIMITS, **{field: 1}))
        assert result.report.outcome == "exhausted" and not result.report.complete
    shared = Budget(CONTROL_LIMITS)
    assert run_reproduction(request, budget=shared).report.complete
    assert shared.remaining("work") < CONTROL_LIMITS.max_work
    cancelled = Event()
    cancelled.set()
    assert run_reproduction(request, cancel=cancelled).report.outcome == "exhausted"
    assert run_reproduction(request, timeout=0.001).report.outcome == "exhausted"
    assert run_reproduction(request, limits=CONTROL_LIMITS).report.complete


def test_optional_native_backends_cannot_be_manufactured_into_signatures() -> None:
    if importlib.util.find_spec("pyld") is not None:
        return
    request = _request()
    result = run_reproduction(request, limits=CONTROL_LIMITS)
    assert not result.report.complete and result.report.issues
    with pytest.raises(ReproductionUnavailable):
        failure_signature(request, result, result.report.issues[0])


def test_completed_native_failure_survives_inherited_receipt_cancellation() -> None:
    """Keep actual completed context findings while settling their native costs."""
    pytest.importorskip("pyld")
    request = _request()
    baseline = run_reproduction(request, limits=CONTROL_LIMITS)
    assert baseline.report.complete and baseline.report.outcome == "disagreement"
    cancellation = Event()
    ledger = Budget(CONTROL_LIMITS)
    parent = Operation(CONTROL_LIMITS, ledger, 30, cancellation)
    native: list[bytes] = []
    before: dict[BudgetKind, int] = {}
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        returned: object,
    ) -> None:
        if previous is not None:
            previous(frame, event, returned)
        if (
            event == "return"
            and frame.f_code is execute_worker.__code__
            and frame.f_locals.get("module")
            == "bijux_ontology.workflows.reproductions.worker"
            and isinstance(returned, bytes)
        ):
            native.append(returned)
            before.update((kind, ledger.remaining(kind)) for kind in BUDGET_KINDS)
            cancellation.set()

    sys.setprofile(observe)
    try:
        completed = run_reproduction(request, budget=parent)
    finally:
        sys.setprofile(previous)
    assert (
        len(native) == 1
        and json.loads(native[0])["report"]["outcome"] == "disagreement"
    )
    assert completed.report.complete
    assert completed.report.issues == baseline.report.issues
    consumed = json.loads(native[0])["consumed"]
    assert all(
        ledger.remaining(kind) == before[kind] - consumed[kind] for kind in BUDGET_KINDS
    )
    cancellation.clear()
    recovered = run_reproduction(request, budget=parent)
    assert (
        recovered.report.complete and recovered.report.issues == baseline.report.issues
    )
