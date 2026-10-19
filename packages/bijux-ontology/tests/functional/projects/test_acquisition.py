"""Real opt-in HTTP closures become ordinary portable offline project bundles."""

import gzip
import importlib.util
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.expansion import expand_document
from bijux_ontology.workflows.projects.acquisition import (
    AcquisitionPolicy,
    AcquisitionRequest,
    acquire_bundle,
)
from bijux_ontology.workflows.projects.acquisition.http import HttpAcquisition
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
