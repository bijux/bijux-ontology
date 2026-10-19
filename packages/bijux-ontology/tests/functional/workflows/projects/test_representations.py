"""Actual HTTP negotiation, selected RDF agreement and required resource truth."""

import hashlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.workflows.projects.acquisition import AcquisitionPolicy
from bijux_ontology.workflows.projects.representations import (
    PublishedResource,
    RepresentationVariant,
    check_representations,
)

Response = tuple[int, dict[str, str], bytes]
Dispatch = Callable[[str, str, str], Response]


@contextmanager
def _server(dispatch: Dispatch) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """Run actual local sockets while preserving the original observed Accept."""
    contacts: list[tuple[str, str]] = []
    base = ""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Serve caller-owned fixture bytes without another RDF implementation."""
            accept = self.headers.get("Accept", "")
            contacts.append((self.path, accept))
            status, headers, body = dispatch(self.path, accept, base)
            try:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Cancellation deliberately closes the actual worker connection.
                pass

        def log_message(self, format: str, *args: object) -> None:
            """Keep fixture request evidence in its owned contact inventory."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, contacts
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _variants() -> tuple[RepresentationVariant, ...]:
    """Declare exact required root media; request headers never prove the response."""
    return (
        RepresentationVariant("turtle", "text/turtle", "text/turtle"),
        RepresentationVariant("json", "application/ld+json", "application/ld+json"),
    )


def _simple(path: str, accept: str, base: str) -> Response:
    """Represent the same finite assertion through two genuine RDF syntaxes."""
    if accept == "text/turtle":
        return 200, {"Content-Type": "text/turtle"}, b"<urn:s> <urn:p> <urn:o> ."
    return (
        200,
        {"Content-Type": "application/ld+json"},
        b'{"@id":"urn:s","urn:p":{"@id":"urn:o"}}',
    )


def test_actual_negotiation_shared_blanks_lists_and_qualified_redirects() -> None:
    """Different presentation and blank labels preserve actual joint RDF topology."""
    # Positive representation coverage includes owned parsing and report handoff.
    turtle = (
        b'<urn:s> <urn:p> _:node; <urn:q> _:node; <urn:list> ("a" "b") . '
        b'_:node <urn:value> "01"^^<http://www.w3.org/2001/XMLSchema#integer> .'
    )
    document = [
        {
            "@id": "urn:s",
            "urn:p": {"@id": "_:other"},
            "urn:q": {"@id": "_:other"},
            "urn:list": {"@list": ["a", "b"]},
        },
        {
            "@id": "_:other",
            "urn:value": {
                "@value": "01",
                "@type": "http://www.w3.org/2001/XMLSchema#integer",
            },
        },
    ]
    encoded = json.dumps(document).encode()

    def dispatch(path: str, accept: str, base: str) -> Response:
        return (
            200,
            {
                "Content-Type": "text/turtle"
                if accept == "text/turtle"
                else "application/ld+json"
            },
            turtle if accept == "text/turtle" else encoded,
        )

    with _server(dispatch) as (base, contacts):
        result = check_representations(
            PublishedResource("urn:publication", base + "/resource", _variants()),
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert result.report.verified and result.scope == "complete"
        assert [value.requests for value in result.variants] == [1, 1]
        assert contacts == [
            ("/resource", "text/turtle"),
            ("/resource", "application/ld+json"),
        ]
        assert result.variants[0].resource is not None
        assert result.variants[1].resource is not None
        assert result.variants[0].resource.sha256 == hashlib.sha256(turtle).hexdigest()
        assert result.variants[1].resource.sha256 == hashlib.sha256(encoded).hexdigest()
        assert result.variants[1].report is not None
        assert any(
            child.scope == "rdf.comparison"
            for child in result.variants[1].report.children
        )

    def redirected(path: str, accept: str, base: str) -> Response:
        if path == "/resource":
            target = (
                "/actual/turtle/file"
                if accept == "text/turtle"
                else "/actual/json/file"
            )
            return 302, {"Location": target}, b""
        if accept == "text/turtle":
            return (
                200,
                {"Content-Type": "text/turtle"},
                b"<../entity> <urn:p> <../value> .",
            )
        return (
            200,
            {"Content-Type": "application/ld+json"},
            (b'{"@id":"../entity","urn:p":{"@id":"../value"}}'),
        )

    with _server(redirected) as (base, contacts):
        variants = (
            RepresentationVariant(
                "turtle",
                "text/turtle",
                "text/turtle",
                effective_url=base + "/actual/turtle/file",
            ),
            RepresentationVariant(
                "json",
                "application/ld+json",
                "application/ld+json",
                effective_url=base + "/actual/json/file",
            ),
        )
        actual = check_representations(
            PublishedResource("urn:logical:publication", base + "/resource", variants),
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert actual.report.verified and len(contacts) == 4
        assert sum(value.requests for value in actual.variants) == 4
        unbound = check_representations(
            PublishedResource(
                "urn:logical:publication", base + "/resource", _variants()
            ),
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert not unbound.report.verified
        assert any(
            issue.code == "publication.identity_mismatch"
            for issue in unbound.report.issues
        )


def test_changed_terms_missing_media_profile_and_disallowed_redirect() -> None:
    """Every required variant matters; matching bytes or majority cannot waive it."""
    mode = {"value": "changed"}

    def dispatch(path: str, accept: str, base: str) -> Response:
        if accept == "text/turtle":
            return _simple(path, accept, base)
        if mode["value"] == "missing":
            return 404, {}, b""
        if mode["value"] == "media":
            return 200, {"Content-Type": "text/turtle"}, b"<urn:s> <urn:p> <urn:o> ."
        if mode["value"] == "profile":
            return (
                200,
                {"Content-Type": 'application/ld+json;profile="urn:other"'},
                (b'{"@id":"urn:s","urn:p":{"@id":"urn:o"}}'),
            )
        return (
            200,
            {"Content-Type": "application/ld+json"},
            (b'{"@id":"urn:s","urn:p":{"@id":"urn:changed"}}'),
        )

    with _server(dispatch) as (base, contacts):
        for label in ("changed", "missing", "media", "profile"):
            mode["value"] = label
            variants = (
                _variants()
                if label != "profile"
                else (
                    _variants()[0],
                    RepresentationVariant(
                        "json",
                        'application/ld+json;profile="urn:required"',
                        "application/ld+json",
                        profile="urn:required",
                    ),
                )
            )
            result = check_representations(
                PublishedResource("urn:publication", base + "/resource", variants),
                policy=AcquisitionPolicy((base,)),
            )
            assert not result.report.verified and result.report.requested == 2
            assert result.variants[1].report is not None
            if label == "changed":
                assert any(
                    issue.code == "rdf.structure_changed"
                    for issue in result.report.issues
                )
            if label == "profile":
                assert any(
                    issue.code == "publication.profile_mismatch"
                    for issue in result.report.issues
                )
        assert len(contacts) == 8

    with _server(_simple) as (other, forbidden):

        def redirects(path: str, accept: str, base: str) -> Response:
            return 302, {"Location": other + "/forbidden"}, b""

        with _server(redirects) as (base, _):
            result = check_representations(
                PublishedResource("urn:publication", base + "/resource", _variants()),
                policy=AcquisitionPolicy((base,)),
            )
            assert not result.report.verified and forbidden == []


def test_real_context_link_imports_and_aggregate_dependency_requests() -> None:
    """Actual processor/import requests use explicit bounded acquisition closure."""
    mode = {"missing": False}

    def dispatch(path: str, accept: str, base: str) -> Response:
        if path == "/json-import":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                b'{"@id":"urn:Term","@type":"http://www.w3.org/2002/07/owl#Class"}',
            )
        if path == "/native-json":
            return (
                200,
                {"Content-Type": "text/turtle"},
                (
                    "<urn:publication> <http://www.w3.org/2002/07/owl#imports> "
                    f"<{base}/json-import> ."
                ).encode(),
            )
        if path == "/linked-context":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                (b'{"@context":{"p":{"@id":"urn:p","@type":"@id"}}}'),
            )
        if path == "/linked":
            if accept == "text/turtle":
                return _simple(path, accept, base)
            return (
                200,
                {
                    "Content-Type": "application/json",
                    "Link": (
                        '</linked-context>; rel="http://www.w3.org/ns/json-ld#context"'
                    ),
                },
                b'{"@id":"urn:s","p":"urn:o"}',
            )
        if path == "/contexts/start":
            return 302, {"Location": "/contexts/selected/current"}, b""
        if path == "/contexts/selected/current":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                (b'{"@context":{"@import":"base"}}'),
            )
        if path == "/contexts/selected/base":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                (b'{"@context":{"p":{"@id":"urn:p","@type":"@id"}}}'),
            )
        if path.startswith("/imports/"):
            if mode["missing"]:
                return 404, {}, b""
            body = "<urn:Term> a <http://www.w3.org/2002/07/owl#Class> ."
            if path == "/imports/root":
                body += (
                    "<urn:imports> <http://www.w3.org/2002/07/owl#imports> "
                    f"<{base}/imports/leaf> ."
                )
            return 200, {"Content-Type": "text/turtle"}, body.encode()
        if accept == "text/turtle":
            return (
                200,
                {"Content-Type": "text/turtle"},
                (
                    "<urn:publication> <http://www.w3.org/2002/07/owl#imports> "
                    f"<{base}/imports/root> . "
                    f"<{base}/s> <urn:p> <{base}/o> ."
                ).encode(),
            )
        return (
            200,
            {"Content-Type": "application/ld+json"},
            json.dumps(
                [
                    {
                        "@context": base + "/contexts/start",
                        "@id": base + "/s",
                        "p": base + "/o",
                    },
                    {
                        "@id": "urn:publication",
                        "http://www.w3.org/2002/07/owl#imports": {
                            "@id": base + "/imports/root"
                        },
                    },
                ]
            ).encode(),
        )

    with _server(dispatch) as (base, contacts):
        contract = PublishedResource("urn:publication", base + "/resource", _variants())
        actual = check_representations(
            contract,
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert actual.report.verified
        assert sum(value.requests for value in actual.variants) == 9
        assert len(contacts) == 9
        limited = check_representations(
            contract,
            policy=AcquisitionPolicy((base,), max_requests=8),
            limits=Limits(max_work=2_000_000),
        )
        assert limited.report.outcome == "exhausted"
        assert sum(value.requests for value in limited.variants) <= 8
        mode["missing"] = True
        missing = check_representations(
            contract,
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert not missing.report.verified
        linked = check_representations(
            PublishedResource(
                "urn:publication",
                base + "/linked",
                (
                    _variants()[0],
                    RepresentationVariant(
                        "json", "application/json", "application/json"
                    ),
                ),
            ),
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=2_000_000),
        )
        assert linked.report.verified and linked.variants[1].resource is not None
        assert linked.variants[1].resource.context_url == base + "/linked-context"
        imported = check_representations(
            PublishedResource(
                "urn:publication",
                base + "/native-json",
                (
                    _variants()[0],
                    RepresentationVariant(
                        "alternate", "text/turtle;q=.9", "text/turtle"
                    ),
                ),
            ),
            policy=AcquisitionPolicy((base,)),
            limits=Limits(max_work=4000000),
        )
        assert imported.report.verified
        assert sum(value.requests for value in imported.variants) == 4


def test_complete_empty_graph_inventory_and_explicit_projection() -> None:
    """Default-graph agreement cannot manufacture complete dataset agreement."""
    mode = {"empty": True}

    def dispatch(path: str, accept: str, base: str) -> Response:
        if accept == "application/trig":
            return (
                200,
                {"Content-Type": "application/trig"},
                (b"<urn:s> <urn:p> <urn:o> . <urn:empty> {}"),
            )
        document = [{"@id": "urn:s", "urn:p": {"@id": "urn:o"}}]
        if mode["empty"]:
            document.append({"@id": "urn:empty", "@graph": []})
        return (
            200,
            {"Content-Type": "application/ld+json"},
            json.dumps(document).encode(),
        )

    with _server(dispatch) as (base, _):
        variants = (
            RepresentationVariant("trig", "application/trig", "application/trig"),
            _variants()[1],
        )
        complete = PublishedResource("urn:publication", base + "/resource", variants)
        assert check_representations(
            complete, policy=AcquisitionPolicy((base,))
        ).report.verified
        mode["empty"] = False
        loss = check_representations(complete, policy=AcquisitionPolicy((base,)))
        assert not loss.report.verified
        assert any(
            issue.code == "rdf.structure_changed" for issue in loss.report.issues
        )
        projection = check_representations(
            PublishedResource(
                "urn:publication", base + "/resource", variants, graph_name="@default"
            ),
            policy=AcquisitionPolicy((base,)),
        )
        assert projection.report.verified and projection.scope == "graph-projection"
        assert projection.graph_name == "@default"
        assert projection.report.regime == "rdf-publication-graph-projection"


def test_bounds_known_prefix_cumulative_budget_and_active_cancellation() -> None:
    """Paid mismatch survives missing work; cancellation reaps before returning."""
    started, release, cancel = threading.Event(), threading.Event(), threading.Event()
    mode = {"slow": False, "changed": False, "wrong_media": False}

    def dispatch(path: str, accept: str, base: str) -> Response:
        if mode["slow"]:
            started.set()
            release.wait(5)
        if mode["wrong_media"]:
            body = [
                {"@id": f"urn:s:{index}", "urn:p": {"@id": f"urn:o:{index}"}}
                for index in range(80)
            ]
            return (
                200,
                {"Content-Type": "application/ld+json"},
                json.dumps(body).encode(),
            )
        if mode["changed"] and accept != "text/turtle":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                (b'{"@id":"urn:s","urn:p":{"@id":"urn:changed"}}'),
            )
        return _simple(path, accept, base)

    with _server(dispatch) as (base, contacts):
        policy = AcquisitionPolicy((base,))
        contract = PublishedResource("urn:publication", base + "/resource", _variants())
        for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=10)):
            limited = check_representations(contract, policy=policy, limits=limits)
            assert limited.report.outcome == "exhausted"
        oversized = PublishedResource(
            "urn:publication",
            base + "/resource",
            (
                _variants()[0],
                RepresentationVariant(
                    "x" * 100000,
                    "application/ld+json",
                    "application/ld+json",
                ),
            ),
        )
        bounded = check_representations(
            oversized,
            policy=policy,
            limits=Limits(max_bytes=1000),
        )
        assert bounded.report.outcome == "exhausted"
        assert bounded.report.requested == 2
        assert all(len(value.variant.name) < 1000 for value in bounded.variants)
        assert contacts == []
        mode["changed"] = True
        extended = PublishedResource(
            "urn:publication",
            base + "/resource",
            (
                *_variants(),
                RepresentationVariant(
                    "third", "application/ld+json", "application/ld+json"
                ),
            ),
        )
        prefix = check_representations(
            extended,
            policy=AcquisitionPolicy((base,), max_requests=2),
            limits=Limits(max_issues=1),
        )
        assert prefix.report.outcome == "exhausted"
        assert any(
            issue.code == "rdf.structure_changed" for issue in prefix.report.issues
        )
        assert prefix.variants[2].report is None
        mode["changed"] = False
        mode["wrong_media"] = True
        paid_exhaustion = False
        for cap in (500000, 1000000, 2000000):
            before = len(contacts)
            bounded_prefix = check_representations(
                contract,
                policy=policy,
                limits=Limits(max_work=cap),
            )
            if len(contacts) - before == 2:
                assert any(
                    issue.code == "publication.media_mismatch"
                    for issue in bounded_prefix.report.issues
                )
                paid_exhaustion |= bounded_prefix.report.outcome == "exhausted"
        assert paid_exhaustion
        mode["wrong_media"] = False
        shared = Budget(Limits())
        assert check_representations(
            contract, policy=policy, budget=shared
        ).report.verified
        shared.consume("work", shared.remaining("work"))
        before = len(contacts)
        assert (
            check_representations(contract, policy=policy, budget=shared).report.outcome
            == "exhausted"
        )
        assert len(contacts) == before
        mode["slow"] = True
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                check_representations(contract, policy=policy, cancel=cancel)
            )
        )
        thread.start()
        assert started.wait(5)
        cancel.set()
        thread.join(5)
        release.set()
        assert not thread.is_alive() and results[0].report.outcome == "exhausted"
        mode["slow"] = False
        assert check_representations(contract, policy=policy).report.verified


def test_protocol_evidence_capacity_precedes_paid_metadata_and_raw_children() -> None:
    """Large response evidence stops before it can erase earlier paid findings."""
    mode = {"small_first": False, "raw_child": False}

    def dispatch(path: str, accept: str, base: str) -> Response:
        if mode["raw_child"] and accept != "application/ld+json; variant=0":
            return (
                200,
                {"Content-Type": "application/ld+json"},
                json.dumps({"@context": {"😀" * 300: {"@id": 1}}}).encode(),
            )
        parameter = (
            ""
            if mode["small_first"] and accept.endswith("variant=0")
            else '; harmless="' + "a" * 45000 + '"'
        )
        return (
            200,
            {"Content-Type": "text/turtle" + parameter},
            b"<urn:s> <urn:p> <urn:o> .",
        )

    with _server(dispatch) as (base, contacts):
        variants = tuple(
            RepresentationVariant(
                str(index),
                f"application/ld+json; variant={index}",
                "application/ld+json",
            )
            for index in range(6)
        )
        contract = PublishedResource("urn:publication", base + "/resource", variants)
        large = check_representations(
            contract, policy=AcquisitionPolicy((base,)), limits=Limits(max_work=4000000)
        )
        assert large.report.outcome == "exhausted" and len(contacts) < 6
        mode["small_first"] = True
        prefix = check_representations(
            contract, policy=AcquisitionPolicy((base,)), limits=Limits(max_work=4000000)
        )
        assert prefix.report.outcome == "exhausted"
        assert any(
            issue.code == "publication.media_mismatch" for issue in prefix.report.issues
        )
        mode["raw_child"] = True
        nested = check_representations(
            contract, policy=AcquisitionPolicy((base,)), limits=Limits(max_work=4000000)
        )
        assert not nested.report.verified
        assert any(
            issue.code == "publication.media_mismatch" for issue in nested.report.issues
        )
        # Every raw source finding that was paid remains in the aggregate prefix.
        aggregate = set(nested.report.issues)
        for variant in nested.variants:
            if variant.report is not None:
                for child in variant.report.children:
                    assert all(
                        issue in aggregate
                        for issue in child.issues
                        if issue.kind != "exhausted"
                    )
