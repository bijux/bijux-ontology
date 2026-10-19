"""Maintained framing retains explicit projection contracts and actual bindings."""

import copy
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest
from rdflib import RDF, XSD, Literal, URIRef

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.execution import WorkerExecutor
from bijux_ontology.json.paths import at_path
from bijux_ontology.jsonld.framing import FramedDocument, frame_document
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.selectors import EntitySelector
from bijux_ontology.models.emission import ModelUsage, check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.comparison import compare_rdf

E = "urn:framing:"
CONTEXT = {
    "@vocab": E,
    "child": E + "child",
    "shared": E + "shared",
    "members": {"@id": E + "members", "@container": "@list"},
}
PREDICATES = (str(RDF.type), E + "label", E + "child", E + "shared", E + "members")


def catalogue() -> tuple[dict[str, object], dict[str, object]]:
    """Supply distinct named records with a shared anonymous child and RDF list."""
    document: dict[str, object] = {
        "@context": CONTEXT,
        "@graph": [
            {
                "@id": E + "record",
                "@type": "Record",
                "label": "Record",
                "child": {
                    "@id": E + "child",
                    "label": {
                        "@value": "01",
                        "@type": str(XSD.integer),
                    },
                },
                "shared": {
                    "@id": "_:shared",
                    "label": {"@value": "Shared", "@language": "en"},
                },
                "members": ["A", "B", "A"],
                "note": "permitted omission",
            },
            {
                "@id": E + "other",
                "@type": "Record",
                "label": "Other",
                "shared": {"@id": "_:shared"},
            },
            {"@id": E + "outside", "@type": "Excluded", "label": "Outside"},
        ],
    }
    frame: dict[str, object] = {
        "@context": CONTEXT,
        "@type": "Record",
        "@explicit": True,
        "@omitDefault": True,
        "label": {},
        "child": {"@explicit": True, "label": {}},
        "shared": {"@explicit": True, "label": {}},
        "members": {},
    }
    return document, frame


def project(
    document: object,
    frame: object,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FramedDocument:
    """Keep the declared retained roots and broad allowed omissions explicit."""
    return frame_document(
        document,
        frame,
        retained=EntitySelector(
            identifiers=(E + "record", E + "other", E + "child"), graph=None
        ),
        predicates=PREDICATES,
        omitted=(EntitySelector(identifiers=(E + "outside",), graph=None),),
        omitted_predicates=(E + "note",),
        limits=limits,
        budget=budget,
    )


def test_catalogue_projection_retains_joint_lists_literals_and_real_model_paths() -> (
    None
):
    """A selected projection can pass while whole-dataset comparison disagrees."""
    document, frame = catalogue()
    original = copy.deepcopy(document)
    result = project(document, frame)
    assert result.report.verified, result.report.to_dict()
    assert result.report.regime == "selected-jsonld-projection"
    assert result.source is not None
    assert result.base is None
    assert document == original
    output = result.document
    assert isinstance(output, dict)
    child = result.bind(E + "child")
    assert child.paths
    actual_child = cast(dict[str, object], at_path(output, child.paths[0]))
    assert cast(dict[str, object], actual_child["label"])["@value"] == "01"
    models = (
        ModelDeclaration(
            "child",
            (),
            (
                FieldDeclaration(
                    "label",
                    E + "label",
                    (ValueDeclaration("literal", str(XSD.integer)),),
                    emission="guaranteed",
                ),
            ),
        ),
    )
    assert check_emission(
        output,
        models,
        tuple(ModelUsage("child", path) for path in child.paths),
        base=result.base,
    ).verified
    assert check_rdf_emission(
        result.source,
        models,
        (RdfModelUsage("child", URIRef(E + "child")),),
        graph_name="@default",
    ).verified
    graph = result.source.graph("@default")
    left = next(graph.objects(URIRef(E + "record"), URIRef(E + "shared")))
    right = next(graph.objects(URIRef(E + "other"), URIRef(E + "shared")))
    assert left == right
    assert (
        URIRef(E + "child"),
        URIRef(E + "label"),
        Literal("01", datatype=XSD.integer, normalize=False),
    ) in graph
    before = prepare_jsonld(document)
    assert before.source is not None
    assert compare_rdf(before.source, result.source).outcome == "disagreement"
    assert any(
        item.predicates is None and item.identifier == E + "outside"
        for item in result.omissions
    )
    output.clear()
    assert result.document


def test_nested_list_and_required_named_record_losses_cannot_return_output() -> None:
    """A valid frame losing selected content fails the required RDF contract."""
    document, frame = catalogue()
    for field in ("child", "shared", "members"):
        missing = copy.deepcopy(frame)
        missing[field] = (
            {"@explicit": True}
            if field != "members"
            else {"@omitDefault": True, "@default": []}
        )
        if field == "members":
            del missing[field]
        result = project(document, missing)
        assert not result.report.verified, field
        assert result.document is None and result.source is None
        assert result.report.outcome != "input_error", result.report.to_dict()


def test_unclassified_omission_overlap_missing_and_empty_required_scope_fail() -> None:
    """Exceptions cannot erase required scope or silently admit other records."""
    document, frame = catalogue()
    retained = EntitySelector(
        identifiers=(E + "record", E + "other", E + "child"), graph=None
    )
    cases = (
        {"omitted": (), "omitted_predicates": (E + "note",)},
        {
            "omitted": (EntitySelector(identifiers=(E + "outside",), graph=None),),
            "omitted_predicates": (E + "note", E + "unexercised"),
        },
        {"omitted": (EntitySelector(identifiers=(E + "outside",), graph=None),)},
        {"omitted": (EntitySelector(identifiers=(E + "record",), graph=None),)},
        {"retained": replace(retained, identifiers=(E + "absent",)), "omitted": ()},
        {
            "retained": replace(retained, identifiers=(E + "absent",), minimum=0),
            "omitted": (),
        },
    )
    for case in cases:
        options: dict[str, object] = {"retained": retained, "predicates": PREDICATES}
        options.update(case)
        result = frame_document(document, frame, **options)  # type: ignore[arg-type]
        assert not result.report.verified
        assert result.document is None


def test_physical_graph_and_effective_remote_base_remain_explicit() -> None:
    """Same IDs in another graph cannot satisfy selected-graph requirements."""
    graph = E + "graph"
    document = {
        "@context": CONTEXT,
        "@graph": [
            {"@id": graph, "@graph": [{"@id": E + "record", "label": "Selected"}]},
            {
                "@id": E + "elsewhere",
                "@graph": [{"@id": E + "record", "label": "Other"}],
            },
        ],
    }
    frame = {
        "@context": CONTEXT,
        "@id": graph,
        "@graph": {"@id": E + "record", "label": {}},
    }
    result = frame_document(
        document,
        frame,
        retained=EntitySelector(identifiers=(E + "record",), graph=graph),
        predicates=(E + "label",),
    )
    assert result.report.verified, result.report.to_dict()
    assert result.source is not None and result.source.graph_name is None
    assert result.bind(E + "record", graph=graph).paths
    merged = frame_document(
        document,
        {"@context": CONTEXT, "@id": E + "record", "label": {}},
        retained=EntitySelector(identifiers=(E + "record",), graph=graph),
        predicates=(E + "label",),
    )
    assert not merged.report.verified and merged.document is None
    calls: list[str] = []
    url = "https://example.org/requested"
    effective = "https://example.org/effective/input.jsonld"
    resource: dict[str, object] = {
        "@context": {"p": "urn:p"},
        "@id": "record",
        "p": "Text",
    }

    def loader(requested: str, options: dict[str, object]) -> dict[str, object]:
        """Return an owned redirect response without network access."""
        calls.append(requested)
        return {"contextUrl": None, "documentUrl": effective, "document": resource}

    redirected = frame_document(
        url,
        {
            "@context": {"p": "urn:p"},
            "@id": effective.rsplit("/", 1)[0] + "/record",
            "p": {},
        },
        retained=EntitySelector(
            identifiers=("https://example.org/effective/record",), graph=None
        ),
        predicates=("urn:p",),
        document_loader=loader,
    )
    assert redirected.report.verified, redirected.report.to_dict()
    assert redirected.base == effective and calls == [url]
    assert redirected.source is not None
    assert prepare_jsonld(redirected.document, base=redirected.base).report.verified


def test_limits_resource_failures_and_unexpected_loader_errors_stay_distinct() -> None:
    """Required failures retain honest scope and no partially accepted artifact."""
    document, frame = catalogue()
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_bytes=100),
        Limits(max_total_bytes=100),
    ):
        result = project(document, frame, limits=limits)
        assert result.report.outcome == "exhausted", result.report.to_dict()
        assert result.document is None
    missing = frame_document(
        "https://example.org/missing",
        {},
        retained=EntitySelector(identifiers=(E + "record",), graph=None),
        predicates=(E + "label",),
    )
    assert missing.report.outcome == "input_error" and missing.document is None

    def broken(url: str, options: dict[str, object]) -> dict[str, object]:
        """Caller programmer failures are not converted to semantic negatives."""
        raise RuntimeError("ordinary caller failure")

    with pytest.raises(RuntimeError, match="ordinary caller"):
        frame_document(
            document,
            "https://example.org/frame",
            retained=EntitySelector(identifiers=(E + "record",), graph=None),
            predicates=PREDICATES,
            document_loader=broken,
        )
    operation = Budget()
    project(document, frame, budget=operation)
    assert operation.remaining("work") < operation.limits.max_work


def test_metadata_bounds_prefix_findings_and_added_required_defaults() -> None:
    """Hostile metadata cannot escape limits or turn a partial result green."""
    document, frame = catalogue()
    oversized = "https://example.org/" + "x" * 100_000
    for options in (
        {"base": oversized},
        {"retained": EntitySelector(identifiers=(E + "record",), graph=oversized)},
        {"omitted": (EntitySelector(identifiers=(E + "outside",), graph=oversized),)},
    ):
        arguments: dict[str, object] = {
            "retained": EntitySelector(identifiers=(E + "record",), graph=None),
            "predicates": PREDICATES,
            "limits": Limits(max_bytes=2000),
        }
        arguments.update(options)
        result = frame_document(document, frame, **arguments)  # type: ignore[arg-type]
        assert result.report.outcome == "exhausted", result.report.to_dict()
        assert result.document is None and result.base is None
        assert oversized not in str(result.report.to_dict())
    count = frame_document(
        document,
        frame,
        retained=EntitySelector(
            identifiers=(E + "record", E + "other", E + "child"), graph=None, minimum=4
        ),
        predicates=PREDICATES,
        omitted=(EntitySelector(identifiers=(E + "outside",), graph=None),),
        omitted_predicates=(E + "note",),
        limits=Limits(max_issues=1),
    )
    assert not count.report.verified and count.document is None
    assert any(issue.code == "selector.count_mismatch" for issue in count.report.issues)
    assert (
        len([issue for issue in count.report.issues if issue.kind != "exhausted"]) <= 1
    )
    # Defaults on a REQUIRED predicate add actual RDF and cannot pass retention.
    simple = {"@id": E + "record", E + "label": "Original"}
    added = frame_document(
        simple,
        {"@id": E + "record", E + "extra": {"@default": "invented"}},
        retained=EntitySelector(identifiers=(E + "record",), graph=None),
        predicates=(E + "label", E + "extra"),
    )
    assert added.report.outcome == "disagreement" and added.document is None


def test_worker_projection_parity_deadline_cancellation_and_recovery(
    tmp_path: Path,
) -> None:
    """Explicit isolation bounds worker lifetime without in-process timeout claims."""
    document, frame = catalogue()
    request: dict[str, object] = {
        "document": document,
        "frame": frame,
        "retained": {
            "kind": "entity",
            "identifiers": [E + "record", E + "other", E + "child"],
            "graph": None,
        },
        "predicates": list(PREDICATES),
        "omitted": [{"kind": "entity", "identifiers": [E + "outside"], "graph": None}],
        "omitted_predicates": [E + "note"],
    }
    executor = WorkerExecutor(artifacts=tmp_path)
    checked = executor.run("framing", request)
    direct = project(document, frame)
    assert checked.report == direct.report and checked.report.verified
    assert isinstance(checked.data, dict)
    assert checked.data["document"] == direct.document
    assert checked.data["base"] == direct.base
    assert checked.data["nodes"]
    timed = WorkerExecutor(artifacts=tmp_path, timeout=0.001).run("framing", request)
    assert timed.report.outcome == "exhausted"
    assert any(issue.code == "execution.deadline" for issue in timed.report.issues)
    cancelled = Event()
    stop = Event()

    def cancel_live_request() -> None:
        """Interrupt an actual isolated request once its bounded input exists."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not stop.is_set():
            if tuple(tmp_path.glob("*/request.json")):
                stop.wait(0.05)
                cancelled.set()
                return
            stop.wait(0.005)

    watcher = Thread(target=cancel_live_request)
    watcher.start()
    try:
        interrupted = executor.run("framing", request, cancellation=cancelled)
    finally:
        stop.set()
        watcher.join()
    assert interrupted.report.outcome == "exhausted"
    assert any(
        issue.code == "execution.cancelled" for issue in interrupted.report.issues
    )
    assert executor.run("framing", request).report.verified
    assert not tuple(tmp_path.iterdir())


def test_framing_input_capture_stops_before_allocating_depleted_payload() -> None:
    """Framing reuses the same prequalified JSON capture as resource ownership."""
    document = {"@context": {"@vocab": E}, "@id": E + "record", "label": "x" * 500000}
    tracemalloc.start()
    try:
        result = frame_document(
            document,
            {},
            retained=EntitySelector(identifiers=(E + "record",), graph=None),
            predicates=(E + "label",),
            budget=Budget(Limits(max_total_bytes=32768)),
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.report.outcome == "exhausted" and not result.report.verified
    assert peak < 100000


def test_inline_frame_and_document_keep_their_explicit_local_base() -> None:
    base = "https://example.org/framing/"
    context = {"@base": base, "label": base + "label"}
    document = {"@context": context, "@id": "subject", "label": "Label"}
    framed = frame_document(
        document,
        {"@context": context, "@id": "subject", "label": {}},
        retained=EntitySelector(identifiers=(base + "subject",), graph=None),
        predicates=(base + "label",),
    )
    assert framed.report.verified, framed.report.to_dict()
    assert framed.document is not None
    with pytest.raises(ResourceError, match="without a semantic input diagnostic"):
        frame_document(
            document,
            {"@context": {**context, "@base": "../"}, "@id": "subject", "label": {}},
            retained=EntitySelector(identifiers=(base + "subject",), graph=None),
            predicates=(base + "label",),
        )


def test_retained_names_share_one_actual_source_association_walk() -> None:
    """Measure actual source traversal without replacing framing or observations."""
    import sys
    from types import FrameType

    roots: dict[int, FrameType] = {}

    def recording(frame: FrameType, event: str, argument: object) -> None:
        if (
            event == "call"
            and frame.f_code.co_name == "walk"
            and "source_associations" in frame.f_code.co_qualname
            and frame.f_locals.get("path") == ()
        ):
            roots[id(frame)] = frame

    previous = sys.getprofile()
    sys.setprofile(recording)
    try:
        result = project(*catalogue())
    finally:
        sys.setprofile(previous)
    assert result.report.verified, result.report.to_dict()
    assert len(result.nodes) == 3 and all(node.paths for node in result.nodes)
    assert len(roots) == 1
