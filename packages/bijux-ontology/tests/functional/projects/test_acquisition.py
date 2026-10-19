"""Real opt-in HTTP closures become ordinary portable offline project bundles."""

import gzip
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import pytest
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, Limits
from bijux_ontology.execution.processes.ownership import spawn_owned_process
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.jsonld.processing.expansion import expand_document
from bijux_ontology.workflows.projects.acquisition import (
    AcquisitionPolicy,
    AcquisitionRequest,
    acquire_bundle,
)
from bijux_ontology.workflows.projects.acquisition.http import HttpAcquisition
from bijux_ontology.workflows.projects.acquisition.http_transport import (
    _FRAME,
    _REQUEST,
    _receipt,
)
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.checking import check_project

Route = tuple[int, dict[str, str], bytes]
ROOT = (
    b"@prefix owl: <http://www.w3.org/2002/07/owl#> . "
    b"<urn:ontology> owl:imports <child> . "
    b"<urn:C> a <http://www.w3.org/2000/01/rdf-schema#Class> ."
)
CHILD = b"<urn:D> a <http://www.w3.org/2000/01/rdf-schema#Class> ."


def test_fresh_reads_observe_changes_without_resetting_allowances() -> None:
    """Use actual HTTP while keeping ordinary cached resource identity intact."""
    seen: list[tuple[str, dict[str, str]]] = []
    routes: dict[str, Route] = {
        "/version": (200, {"Content-Type": "application/json"}, b'{"version":1}')
    }
    with server(routes, seen) as base:
        budget = Operation(Limits(), None, 5, None)
        client = HttpAcquisition(AcquisitionPolicy((base,), max_requests=3), budget)
        original = client.get(base + "/version")
        routes["/version"] = (
            200,
            {"Content-Type": "application/json"},
            b'{"version":2}',
        )
        assert client.get(base + "/version") is original
        fresh = client.get(base + "/version", fresh=True)
        assert fresh.payload == b'{"version":2}'
        assert client.get(base + "/version") is original
        assert client.get(base + "/version", fresh=True).payload == fresh.payload
        assert len(seen) == client.requests == 3
        assert budget.remaining("resources") == budget.limits.max_resources - 3
        with pytest.raises(LimitExceeded):
            client.get(base + "/version", fresh=True)
        with pytest.raises(InputError):
            client.get(base + "/version", fresh=1)  # type: ignore[arg-type]
        assert len(seen) == 3


@contextmanager
def server(
    routes: dict[str, Route], seen: list[tuple[str, dict[str, str]]]
) -> Iterator[str]:
    """Own a real local HTTP fixture, including its server and worker threads."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep fixture diagnostics inside test assertions."""

        def do_GET(self) -> None:
            """Serve exact bounded fixture bytes and observable request headers."""
            seen.append((self.path, dict(self.headers)))
            if self.path == "/slow":
                time.sleep(1)
            code, headers, payload = routes.get(self.path, (404, {}, b""))
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=fixture.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(fixture.server_port)
    finally:
        fixture.shutdown()
        fixture.server_close()
        worker.join(timeout=2)


def skeleton(path: Path, base: str, *, contexts: bool = False) -> Path:
    """Retain caller-authored checks/local files; acquisition supplies resources."""
    path.mkdir()
    manifest: dict[str, object] = {
        "version": 1,
        "rdf": {
            "root": {"path": "root.ttl", "iri": base + "/root", "format": "turtle"}
        },
        "checks": [
            {
                "name": "terms",
                "kind": "vocabulary",
                "source": "root",
                "inventory": "inventory.json",
            }
        ],
    }
    (path / "inventory.json").write_text(json.dumps(["urn:C", "urn:D"]))
    if contexts:
        manifest["contexts"] = {base + "/context": "context.json"}
        cast(dict[str, object], manifest["rdf"]).update(
            {
                "data": {"path": "data.ttl", "iri": "urn:data"},
                "shapes": {"path": "shapes.ttl", "iri": "urn:shapes"},
            }
        )
        cast(list[object], manifest["checks"]).append(
            {
                "name": "constraints",
                "kind": "shacl",
                "document": "data",
                "shapes": "shapes",
            }
        )
        (path / "data.ttl").write_text('<urn:record> <urn:name> "Record" .')
        (path / "shapes.ttl").write_text(
            "@prefix sh: <http://www.w3.org/ns/shacl#> . "
            "<urn:Shape> a sh:NodeShape; sh:targetNode <urn:record>; "
            "sh:property [sh:path <urn:name>; sh:minCount 1] ."
        )
        cast(list[object], manifest["checks"]).append(
            {
                "name": "jsonld",
                "kind": "context",
                "document": "document.json",
                "expectations": "expectations.json",
            }
        )
        (path / "document.json").write_text(
            json.dumps({"@context": base + "/context", "name": "Record"})
        )
        (path / "expectations.json").write_text(
            json.dumps([{"path": ["name"], "predicate": "urn:name"}])
        )
    (path / "project.json").write_text(json.dumps(manifest))
    return path


def test_real_redirected_import_and_relative_context_bundle_runs_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop acquisition server before loading/moving the ordinary resulting ZIP."""
    if any(importlib.util.find_spec(name) is None for name in ("pyld", "pyshacl")):
        pytest.skip("The actual full bundle requires jsonld and shacl extras")
    seen: list[tuple[str, dict[str, str]]] = []
    routes = {
        "/root": (200, {"Content-Type": "text/turtle"}, ROOT),
        "/child": (302, {"Location": "/final/child"}, b""),
        "/final/child": (
            200,
            {"Content-Type": "text/turtle"},
            CHILD
            + b"<urn:child> <http://www.w3.org/2002/07/owl#imports> <grandchild> . "
            b"<#Versioned> a <http://www.w3.org/2000/01/rdf-schema#Class> .",
        ),
        "/final/grandchild": (200, {"Content-Type": "text/turtle"}, CHILD),
        "/context": (302, {"Location": "/final/context"}, b""),
        "/final/context": (
            200,
            {"Content-Type": "application/ld+json"},
            b'{"@context":["base"]}',
        ),
        "/final/base": (
            200,
            {"Content-Type": "application/ld+json"},
            b'{"@context":{"@import":"base-core"}}',
        ),
    }
    routes["/final/base-core"] = (
        200,
        {"Content-Type": "application/ld+json"},
        b'{"@context":{"name":"urn:name"}}',
    )
    routes["/linked-context"] = (302, {"Location": "/final/linked-context"}, b"")
    routes["/final/linked-context"] = (
        200,
        {
            "Content-Type": "application/json",
            "Link": '</final/base-core>; rel="http://www.w3.org/ns/json-ld#context"',
        },
        b'{"@context":{}}',
    )
    with server(routes, seen) as base:
        source = skeleton(tmp_path / "source", base, contexts=True)
        manifest = json.loads((source / "project.json").read_text())
        manifest["contexts"][base + "/linked-context"] = "linked.json"
        (source / "project.json").write_text(json.dumps(manifest))
        original_manifest = (source / "project.json").read_bytes()
        command = [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "bijux_ontology",
            "acquire",
            str(source),
            "--request",
            "root.ttl",
            base + "/root",
            "--request",
            "context.json",
            base + "/context",
            "--request",
            "linked.json",
            base + "/linked-context",
            "--origin",
            base,
            "--timeout",
            "10",
            "--json",
        ]
        blocked = tmp_path / "blocked.zip"
        rejected = subprocess.run(
            command
            + ["--destination", str(blocked), "--output", str(source / "root.ttl")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode == 2, rejected.stdout + rejected.stderr
        assert not seen and not blocked.exists() and not (source / "root.ttl").exists()
        assert (source / "project.json").read_bytes() == original_manifest
        cli_archive = tmp_path / "cli.zip"
        prepared = subprocess.run(
            command + ["--destination", str(cli_archive)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        assert json.loads(prepared.stdout)["verified"] and cli_archive.is_file()
        result = acquire_bundle(
            source,
            requests=(
                AcquisitionRequest("root.ttl", base + "/root"),
                AcquisitionRequest("context.json", base + "/context"),
                AcquisitionRequest("linked.json", base + "/linked-context"),
            ),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "offline.zip",
        )
        assert result.report.verified, result.report.to_dict()
        assert any(
            item.effective_url == base + "/final/context" for item in result.resources
        )
        linked = tuple(
            item
            for item in result.resources
            if item.effective_url == base + "/final/linked-context"
        )
        assert len(linked) == 2
        assert all(item.context_url == base + "/final/base-core" for item in linked)

    def offline(*args: object, **kwargs: object) -> None:
        raise AssertionError("Ordinary consumption must not access the network")

    monkeypatch.setattr(socket, "create_connection", offline)
    moved = tmp_path / "moved.zip"
    assert result.archive is not None
    result.archive.rename(moved)
    bundle = load_bundle(moved)
    vocabulary = bundle.vocabulary("root")
    assert (URIRef("urn:C"), RDF.type, RDFS.Class) in vocabulary.graph()
    assert (URIRef("urn:D"), RDF.type, RDFS.Class) in vocabulary.graph()
    assert (
        URIRef(base + "/final/child#Versioned"),
        RDF.type,
        RDFS.Class,
    ) in vocabulary.graph()
    expanded = expand_document(
        {"@context": base + "/context", "name": "Record"},
        document_loader=bundle.document_loader,
    )
    assert "urn:name" in expanded[0]
    assert (
        "urn:name"
        in expand_document(
            {"@context": base + "/linked-context", "name": "Record"},
            document_loader=bundle.document_loader,
        )[0]
    )
    assert (
        bundle.document_loader(base + "/linked-context", {})["contextUrl"]
        == base + "/final/base-core"
    )
    assert check_project(bundle.materialize(tmp_path / "materialized")).verified
    cli_moved = tmp_path / "cli-moved.zip"
    cli_archive.rename(cli_moved)
    assert check_project(
        load_bundle(cli_moved).materialize(tmp_path / "cli-project")
    ).verified
    assert {path for path, _ in seen} == set(routes)


def test_origin_credentials_redirect_counts_and_missing_closure_fail_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit redirect authorization precedes contact and no ambient auth leaks."""
    seen: list[tuple[str, dict[str, str]]] = []
    forbidden: list[tuple[str, dict[str, str]]] = []
    with server(
        {"/root": (200, {"Content-Type": "text/turtle"}, CHILD)}, forbidden
    ) as other:
        routes: dict[str, Route] = {
            "/root": (302, {"Location": other + "/root"}, b""),
            "/loop": (302, {"Location": "/loop"}, b""),
            "/auth": (401, {"WWW-Authenticate": 'Basic realm="fixture"'}, b""),
        }
        with server(routes, seen) as base:
            monkeypatch.setenv("http_proxy", other)
            monkeypatch.setenv("HTTP_PROXY", other)
            monkeypatch.setenv("no_proxy", "")
            monkeypatch.setenv("NO_PROXY", "")
            monkeypatch.setenv("NETRC", str(tmp_path / "netrc"))
            (tmp_path / "netrc").write_text(
                "machine 127.0.0.1 login secret password secret\n"
            )
            source = skeleton(tmp_path / "source", base)
            for name, url, policy in (
                ("origin", base + "/root", AcquisitionPolicy((base,))),
                ("loop", base + "/loop", AcquisitionPolicy((base,))),
                ("auth", base + "/auth", AcquisitionPolicy((base,))),
                (
                    "zero",
                    base + "/root",
                    AcquisitionPolicy((base, other), max_redirects=0),
                ),
            ):
                result = acquire_bundle(
                    source,
                    requests=(AcquisitionRequest("root.ttl", url),),
                    policy=policy,
                    destination=tmp_path / (name + ".zip"),
                )
                assert not result.report.verified and result.archive is None
                assert not (tmp_path / (name + ".zip")).exists()
            routes["/root"] = (200, {"Content-Type": "text/turtle"}, ROOT)
            result = acquire_bundle(
                source,
                requests=(AcquisitionRequest("root.ttl", base + "/root"),),
                policy=AcquisitionPolicy((base,)),
                destination=tmp_path / "missing.zip",
            )
            assert result.report.outcome == "input_error" and result.resources
            limited = acquire_bundle(
                source,
                requests=(AcquisitionRequest("root.ttl", base + "/root"),),
                policy=AcquisitionPolicy((base,), max_requests=1),
                destination=tmp_path / "request-cap.zip",
            )
            assert limited.report.outcome == "exhausted" and limited.resources
            assert limited.archive is None
    assert not forbidden
    for _, headers in seen:
        assert not {key.lower() for key in headers} & {
            "authorization",
            "cookie",
            "proxy-authorization",
        }
    with pytest.raises(InputError):
        AcquisitionRequest("root.ttl", "http://secret:secret@example.org/root")


def test_gzip_decoding_size_and_media_bounds_preserve_exact_terms(
    tmp_path: Path,
) -> None:
    """Distinct wire/expanded limits and full gzip validation precede publication."""
    exact = b'<urn:x> <urn:p> "01"^^<http://www.w3.org/2001/XMLSchema#integer> .'
    routes: dict[str, Route] = {
        "/root": (
            200,
            {"Content-Type": "text/turtle", "Content-Encoding": "gzip"},
            gzip.compress(exact),
        )
    }
    seen: list[tuple[str, dict[str, str]]] = []
    with server(routes, seen) as base:
        source = skeleton(tmp_path / "source", base)
        result = acquire_bundle(
            source,
            requests=(AcquisitionRequest("root.ttl", base + "/root"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "good.zip",
        )
        assert result.report.verified and result.archive is not None
        graph = load_bundle(result.archive).vocabulary("root").graph()
        assert str(next(graph.objects(URIRef("urn:x"), URIRef("urn:p")))) == "01"
        for name, body, media, limits in (
            ("truncated", gzip.compress(exact)[:-3], "text/turtle", Limits()),
            (
                "expanded",
                gzip.compress(b" " * 10000),
                "text/turtle",
                Limits(max_bytes=5000),
            ),
            ("media", gzip.compress(exact), "text/html", Limits()),
        ):
            routes["/root"] = (
                200,
                {"Content-Type": media, "Content-Encoding": "gzip"},
                body,
            )
            stopped = acquire_bundle(
                source,
                requests=(AcquisitionRequest("root.ttl", base + "/root"),),
                policy=AcquisitionPolicy((base,)),
                destination=tmp_path / (name + ".zip"),
                limits=limits,
            )
            assert not stopped.report.verified and stopped.archive is None


def test_selected_graph_scope_and_request_members_are_explicit(tmp_path: Path) -> None:
    """Unselected graph imports do not authorize new retrievals or union widening."""
    graph = (
        b"<urn:chosen> { <urn:C> a "
        b"<http://www.w3.org/2000/01/rdf-schema#Class> } "
        b"<urn:other> { <urn:o> <http://www.w3.org/2002/07/owl#imports> "
        b"<unselected> }"
    )
    seen: list[tuple[str, dict[str, str]]] = []
    with server(
        {"/root": (200, {"Content-Type": "application/trig"}, graph)}, seen
    ) as base:
        source = skeleton(tmp_path / "source", base)
        manifest = json.loads((source / "project.json").read_text())
        manifest["rdf"]["root"]["format"] = "trig"
        manifest["rdf"]["root"]["graph_name"] = "urn:chosen"
        (source / "project.json").write_text(json.dumps(manifest))
        before = (source / "project.json").read_bytes()
        alias = tmp_path / "source-alias"
        alias.symlink_to(source, target_is_directory=True)
        for destination in (source / "root.ttl", alias / "root.ttl"):
            refused = acquire_bundle(
                source,
                requests=(AcquisitionRequest("root.ttl", base + "/root"),),
                policy=AcquisitionPolicy((base,)),
                destination=destination,
            )
            assert refused.report.outcome == "input_error" and refused.archive is None
            assert not seen and not (source / "root.ttl").exists()
            assert (source / "project.json").read_bytes() == before
        result = acquire_bundle(
            source,
            requests=(AcquisitionRequest("root.ttl", base + "/root"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "selected.zip",
        )
        assert result.report.verified
        assert [path for path, _ in seen] == ["/root"]
        rejected = acquire_bundle(
            source,
            requests=(AcquisitionRequest("undeclared.ttl", base + "/root"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "extra.zip",
        )
        assert not rejected.report.verified and len(seen) == 1


def test_deadline_cancellation_runtime_and_fresh_operation(tmp_path: Path) -> None:
    """A stopped owned worker cannot become a reusable partial bundle."""
    seen: list[tuple[str, dict[str, str]]] = []
    routes = {
        "/root": (200, {"Content-Type": "text/turtle"}, CHILD),
        "/slow": (200, {"Content-Type": "text/turtle"}, CHILD),
    }
    with server(routes, seen) as base:
        source = skeleton(tmp_path / "source", base)
        stopped = threading.Event()
        stopped.set()
        cancelled = acquire_bundle(
            source,
            requests=(AcquisitionRequest("root.ttl", base + "/root"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "cancelled.zip",
            cancel=stopped,
        )
        assert cancelled.report.outcome == "exhausted" and not seen
        start = time.monotonic()
        expired = acquire_bundle(
            source,
            requests=(AcquisitionRequest("root.ttl", base + "/slow"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "timeout.zip",
            timeout=0.3,
        )
        assert expired.report.outcome == "exhausted" and time.monotonic() - start < 1.5
        fresh = acquire_bundle(
            source,
            requests=(AcquisitionRequest("root.ttl", base + "/root"),),
            policy=AcquisitionPolicy((base,)),
            destination=tmp_path / "fresh.zip",
        )
        assert fresh.report.verified
        assert not (tmp_path / "timeout.zip").exists()
    with pytest.raises(InputError):
        AcquisitionRequest("../outside", "http://localhost/root")
    with pytest.raises(InputError):
        AcquisitionPolicy(cast(tuple[str, ...], "http://localhost"))


@contextmanager
def drip_server() -> Iterator[tuple[str, list[str], threading.Event]]:
    """Own real fragmented headers/body and a cooperative fixture shutdown."""
    seen: list[str] = []
    ready, finish = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            with suppress(BrokenPipeError, ConnectionResetError):
                if self.path in ("/headers", "/redirect"):
                    self.connection.sendall(
                        b"HTTP/1.0 302 Found\r\nLocation: /next\r\nX-Drip: "
                        if self.path == "/redirect"
                        else b"HTTP/1.0 200 OK\r\nX-Drip: "
                    )
                    ready.set()
                    for _ in range(40):
                        self.connection.sendall(b"x")
                        if finish.wait(0.03):
                            return
                    self.connection.sendall(b"\r\nContent-Length: 1\r\n\r\nx")
                elif self.path == "/body":
                    self.send_response(200)
                    self.send_header("Content-Length", "40")
                    self.end_headers()
                    ready.set()
                    for _ in range(40):
                        self.wfile.write(b"x")
                        self.wfile.flush()
                        if finish.wait(0.03):
                            return
                elif self.path == "/blocked":
                    self.send_response(200)
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    ready.set()
                    finish.wait(5)
                    self.wfile.write(b"x")
                else:
                    self.send_response(200)
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(b"x")

    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=lambda: fixture.serve_forever(poll_interval=0.01))
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(fixture.server_port), seen, ready
    finally:
        finish.set()
        fixture.shutdown()
        worker.join(2)
        fixture.server_close()
        assert not worker.is_alive()


@pytest.mark.parametrize("part", ("body", "headers", "redirect"))
def test_drip_http_obeys_absolute_deadline_without_opening_late_redirect(
    part: str,
) -> None:
    """Socket activity cannot extend connect/headers/body beyond the same owner."""
    processes: list[subprocess.Popen[bytes]] = []
    previous = sys.getprofile()

    def observe(frame: Any, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code is spawn_owned_process.__code__
            and isinstance(value, subprocess.Popen)
        ):
            processes.append(value)

    with drip_server() as (base, seen, ready):
        operation = Operation(None, None, 0.75, None)
        client = HttpAcquisition(AcquisitionPolicy((base,)), operation)
        sys.setprofile(observe)
        try:
            start = time.monotonic()
            with pytest.raises(LimitExceeded):
                client.get(base + "/" + part)
            elapsed = time.monotonic() - start
            assert ready.is_set() and elapsed < 0.9
            assert seen == ["/" + part] and len(processes) == 1
            assert processes[0].poll() is not None
            if os.name == "posix":
                with pytest.raises(ProcessLookupError):
                    os.kill(processes[0].pid, 0)
            expired = Operation(None, None, 0.001, None)
            threading.Event().wait(0.002)
            with pytest.raises(LimitExceeded):
                HttpAcquisition(AcquisitionPolicy((base,)), expired).get(base + "/ok")
            assert len(processes) == 1
        finally:
            sys.setprofile(previous)


def test_inherited_http_cancellation_reaps_blocked_hop_and_fresh_owner_recovers() -> (
    None
):
    """Inherited cancellation stops socket work; an independent owner recovers."""
    processes: list[subprocess.Popen[bytes]] = []
    previous = sys.getprofile()
    terminal_before: dict[str, int] = {}
    expected: dict[str, int] = {}

    def observe(frame: Any, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code is spawn_owned_process.__code__
            and isinstance(value, subprocess.Popen)
        ):
            processes.append(value)
        if (
            event == "call"
            and frame.f_code is execute_worker.__code__
            and not terminal_before
        ):
            assert frame.f_back is not None
            owned = frame.f_back.f_locals
            terminal_before.update(
                {kind: original._counts.get(kind, 0) for kind in BUDGET_KINDS}
            )
            expected.update(
                {
                    kind: owned["native"][kind]
                    + owned["pipe_grants"][kind]
                    + owned["controls"].consumed()[kind]
                    for kind in BUDGET_KINDS
                }
            )

    with drip_server() as (base, seen, ready):
        cancelled = threading.Event()
        original = Budget()
        parent = Operation(None, original, 5, cancelled)
        child = Operation(None, parent, 30, None)
        at: list[float] = []

        def stop() -> None:
            assert ready.wait(3)
            threading.Event().wait(0.02)
            at.append(time.monotonic())
            cancelled.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        sys.setprofile(observe)
        try:
            client = HttpAcquisition(AcquisitionPolicy((base,)), child)
            with pytest.raises(LimitExceeded, match="cancelled"):
                client.get(base + "/blocked")
            assert at and time.monotonic() - at[0] < 0.15
            stopper.join(1)
            assert not stopper.is_alive() and len(processes) == 1
            assert processes[0].poll() is not None and not client.responses
            assert {
                kind: original._counts.get(kind, 0) - terminal_before[kind]
                for kind in BUDGET_KINDS
            } == expected
            if os.name == "posix":
                with pytest.raises(ProcessLookupError):
                    os.kill(processes[0].pid, 0)
            fresh = HttpAcquisition(
                AcquisitionPolicy((base,)), Operation(None, None, 5, None)
            )
            assert fresh.get(base + "/ok").payload == b"x"
            assert len(processes) == 2 and processes[-1].pid != processes[0].pid
            assert processes[-1].poll() == 0 and seen == ["/blocked", "/ok"]
            assert cancelled.is_set()
        finally:
            sys.setprofile(previous)
            stopper.join(4)


@pytest.mark.parametrize("arrival", ("receipt", "hash"))
def test_completed_http_receipt_does_not_renew_cache_publication(arrival: str) -> None:
    """Already returned native costs settle; later stop forbids resource publication."""
    seen: list[tuple[str, dict[str, str]]] = []
    with server({"/ok": (200, {"Content-Type": "text/plain"}, b"ok")}, seen) as base:
        cancelled = threading.Event()
        original = Budget()
        client = HttpAcquisition(
            AcquisitionPolicy((base,)), Operation(None, original, 5, cancelled)
        )
        marked: list[bool] = []
        previous = sys.getprofile()

        def observe(frame: Any, event: str, value: object) -> None:
            if (
                arrival == "receipt"
                and event == "return"
                and frame.f_code is execute_worker.__code__
                and isinstance(value, bytes)
            ) or (
                arrival == "hash" and event == "c_return" and value is hashlib.sha256
            ):
                marked.append(True)
                cancelled.set()

        sys.setprofile(observe)
        try:
            with pytest.raises(LimitExceeded, match="cancelled"):
                client.get(base + "/ok")
        finally:
            sys.setprofile(previous)
        assert marked and not client.responses and len(seen) == 1
        assert original._counts["resources"] == 1
        assert original._counts["bytes"] > 65536 and original._counts["work"] > 0


def test_maximum_profile_body_keeps_funded_bytes_and_original_work() -> None:
    """The default 5 MB body profile is not shrunk to a JSON decoder's source cap."""
    seen: list[tuple[str, dict[str, str]]] = []
    payload = b"x" * 5_000_000
    with server(
        {
            "/large": (
                200,
                {"Content-Type": "text/plain", "Content-Length": str(len(payload))},
                payload,
            )
        },
        seen,
    ) as base:
        # Hashing already spends one work unit per byte; the same independent
        # explicit work allowance applies before and after native I/O ownership.
        original = Budget(Limits(max_work=6_000_000))
        client = HttpAcquisition(
            AcquisitionPolicy((base,)), Operation(None, original, 10, None)
        )
        response = client.get(base + "/large")
        assert (
            response.payload == payload
            and response.resource.compressed_bytes == len(payload)
        )
        assert original._counts["bytes"] < original.limits.max_total_bytes
        assert original._counts["resources"] == 1


def test_outer_bundle_cancellation_terminates_nested_http_process(
    tmp_path: Path,
) -> None:
    """An outer preparation exit cannot orphan its independent HTTP process group."""
    outer: list[subprocess.Popen[bytes]] = []
    nested: list[int] = []
    cancelled = threading.Event()
    previous = sys.getprofile()

    def observe(frame: Any, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code is spawn_owned_process.__code__
            and isinstance(value, subprocess.Popen)
        ):
            outer.append(value)

    with drip_server() as (base, seen, ready):
        source = skeleton(tmp_path / "source", base)

        def stop() -> None:
            assert ready.wait(4)
            assert outer
            if os.name == "posix":
                rows = subprocess.check_output(
                    ["ps", "-axo", "pid=,ppid=,command="], text=True
                ).splitlines()
                for row in rows:
                    fields = row.strip().split(None, 2)
                    if (
                        len(fields) == 3
                        and int(fields[1]) == outer[0].pid
                        and "bijux_ontology.workflows.projects.acquisition.http_worker"
                        in fields[2]
                    ):
                        nested.append(int(fields[0]))
                assert len(nested) == 1
            cancelled.set()

        stopper = threading.Thread(target=stop)
        stopper.start()
        sys.setprofile(observe)
        try:
            result = acquire_bundle(
                source,
                requests=(AcquisitionRequest("root.ttl", base + "/blocked"),),
                policy=AcquisitionPolicy((base,)),
                destination=tmp_path / "cancelled.zip",
                cancel=cancelled,
                timeout=5,
            )
        finally:
            sys.setprofile(previous)
            stopper.join(5)
        assert not stopper.is_alive() and cancelled.is_set()
        assert (
            result.report.outcome == "exhausted"
            and not (tmp_path / "cancelled.zip").exists()
        )
        assert len(outer) == 1 and outer[0].poll() is not None and seen == ["/blocked"]
        if os.name == "posix":
            assert len(nested) == 1
            end = time.monotonic() + 1
            while True:
                try:
                    os.kill(nested[0], 0)
                except ProcessLookupError:
                    break
                assert time.monotonic() < end, (
                    "Nested HTTP process survived its original parent"
                )
                threading.Event().wait(0.01)


def test_actual_hop_receipt_checks_lengths_counters_and_original_parent_before_io() -> (
    None
):
    """Owned native bytes supply the adversarial wire controls, never fake HTTP."""
    captured: dict[str, Any] = {}
    previous = sys.getprofile()
    seen: list[tuple[str, dict[str, str]]] = []

    def observe(frame: Any, event: str, value: object) -> None:
        if frame.f_code is execute_worker.__code__:
            if event == "call":
                captured["request"] = frame.f_locals["payload"]
                assert frame.f_back is not None
                captured["host"] = frame.f_back.f_locals["host"].copy()
            elif event == "return" and isinstance(value, bytes):
                captured["raw"] = value

    with server({"/ok": (200, {"Content-Type": "text/plain"}, b"ok")}, seen) as base:
        sys.setprofile(observe)
        try:
            client = HttpAcquisition(
                AcquisitionPolicy((base,)), Operation(None, None, 5, None)
            )
            assert client.get(base + "/ok").payload == b"ok"
        finally:
            sys.setprofile(previous)
        raw = captured["raw"]
        size, parent, deadline = _REQUEST.unpack_from(captured["request"])
        data = json.loads(captured["request"][_REQUEST.size :])
        magic, kind, count_size, metadata_size, body_size = _FRAME.unpack_from(raw)
        invalid_length = (
            _FRAME.pack(magic, kind, count_size, metadata_size + 1, body_size)
            + raw[_FRAME.size :]
        )
        owner = Budget()
        with pytest.raises(ResourceError, match="envelope"):
            _receipt(
                invalid_length,
                data["header_cap"],
                data["body_cap"],
                ControlBudget(Limits(), captured["host"]),
            )
        assert not owner._counts
        counters = json.loads(raw[_FRAME.size : _FRAME.size + count_size])
        counters["states"] = True
        encoded = json.dumps(counters, separators=(",", ":")).encode()
        invalid_counter = (
            _FRAME.pack(magic, kind, len(encoded), metadata_size, body_size)
            + encoded
            + raw[_FRAME.size + count_size :]
        )
        _, actual = _receipt(
            invalid_counter,
            data["header_cap"],
            data["body_cap"],
            ControlBudget(Limits(), captured["host"]),
        )
        delegated = DelegatedAllowance(owner, data["grants"])
        with pytest.raises(ResourceError, match="counters"):
            delegated.settle(actual)
        assert not owner._counts
        if os.name == "posix":
            wrong_parent = (
                _REQUEST.pack(size, 0, time.monotonic_ns() + 2_000_000_000)
                + captured["request"][_REQUEST.size :]
            )
            with pytest.raises(ResourceError, match="owner expired"):
                execute_worker(
                    "bijux_ontology.workflows.projects.acquisition.http_worker",
                    wrong_parent,
                    cap=max(4096, len(wrong_parent)),
                    timeout=2,
                )
            assert len(seen) == 1


@pytest.mark.parametrize("stop", ("insertion", "alias_state"))
def test_failed_http_cache_publication_preserves_existing_entries(stop: str) -> None:
    """Paid alias construction and a late stop cannot leave half-published cache."""
    from bijux_ontology.workflows.projects.acquisition.types import AcquiredResource

    seen: list[tuple[str, dict[str, str]]] = []
    routes: dict[str, Route] = {
        "/keep": (200, {"Content-Type": "text/plain"}, b"keep"),
        "/alias": (302, {"Location": "/final"}, b""),
        "/final": (200, {"Content-Type": "text/plain"}, b"final"),
    }
    with server(routes, seen) as base:
        cancelled = threading.Event()
        original = Budget()
        client = HttpAcquisition(
            AcquisitionPolicy((base,)), Operation(None, original, 10, cancelled)
        )
        existing = client.get(base + "/keep")
        inserted: list[str] = []

        class ObservedResponses(dict[str, Any]):
            """Observe the actual insertion without replacing HTTP or result bytes."""

            def __setitem__(self, key: str, value: Any) -> None:
                super().__setitem__(key, value)
                inserted.append(key)
                if stop == "insertion":
                    cancelled.set()

        client.responses = ObservedResponses(client.responses)
        previous = sys.getprofile()
        depleted: list[bool] = []

        def observe(frame: Any, event: str, value: object) -> None:
            if (
                stop == "alias_state"
                and event == "return"
                and frame.f_code is AcquiredResource.__init__.__code__
                and frame.f_locals["self"].requested_url == base + "/alias"
            ):
                original.consume("states", original.remaining("states"))
                depleted.append(True)

        sys.setprofile(observe)
        try:
            with pytest.raises(LimitExceeded):
                client.get(base + "/alias")
        finally:
            sys.setprofile(previous)
        assert dict(client.responses) == {base + "/keep": existing}
        assert bool(inserted) is (stop == "insertion")
        assert bool(depleted) is (stop == "alias_state")
        assert len(seen) == 3
        fresh = HttpAcquisition(
            AcquisitionPolicy((base,)), Operation(None, None, 5, None)
        )
        assert fresh.get(base + "/alias").payload == b"final"
        assert len(seen) == 5
