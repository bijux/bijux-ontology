"""Actual local SPARQL extraction with complete and misleading server receipts."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import parse_qs, urlsplit

import pytest
from rdflib import BNode, Dataset, Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.projects.acquisition import AcquisitionPolicy
from bijux_ontology_adapters.sparql import (
    EndpointCompleteness,
    EndpointQuery,
    acquire_endpoint,
)

P = URIRef("urn:p")
CONTRACT = EndpointCompleteness(
    "revision", "immutable-a", "Snapshot", "Total", "Truncated"
)


@contextmanager
def endpoint(
    mode: str = "complete", *, named: bool = False
) -> Iterator[tuple[str, AcquisitionPolicy, list[dict[str, list[str]]]]]:
    """Run a real maintained SPARQL processor behind bounded HTTP responses."""
    data = Dataset()
    graph = data.graph(URIRef("urn:G")) if named else data.default_graph
    for i in range(5):
        graph.add(
            (
                URIRef("urn:r:" + str(i)),
                P,
                literal_term("0" + str(i), datatype=str(XSD.integer)),
            )
        )
    graph.add((URIRef("urn:Class"), RDF.type, OWL.Class))
    if mode == "blank":
        blank = BNode("shared")
        graph.add((blank, P, Literal("a")))
        graph.add((blank, P, Literal("b")))
    received: list[dict[str, list[str]]] = []

    class Handler(BaseHTTPRequestHandler):
        """Return actual SELECT results and deliberately faulty protocol controls."""

        def log_message(self, format: str, *args: object) -> None:
            """Keep local endpoint output in the caller's evidence."""

        def do_GET(self) -> None:
            """Evaluate the exact received query with maintained RDFLib SPARQL."""
            parameters = parse_qs(urlsplit(self.path).query)
            query = parameters["query"][0]
            received.append(parameters)
            result = graph.query(query) if not named else data.query(query)
            raw = result.serialize(format="json")
            assert isinstance(raw, bytes)
            parsed = json.loads(raw)
            rows = parsed["results"]["bindings"]
            if mode == "cap":
                parsed["results"]["bindings"] = rows[:1]
            if mode == "outside" and rows:
                rows[0]["p"]["value"] = "urn:other"
            if mode == "duplicate" and rows:
                rows.append(rows[0])
            raw = json.dumps(parsed).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/sparql-results+json")
            self.send_header(
                "Content-Length", str(len(raw) + 3 if mode == "wire" else len(raw))
            )
            self.send_header("Connection", "close")
            self.send_header(
                "Snapshot",
                "changed" if mode == "change" and len(received) > 1 else "immutable-a",
            )
            total = len(
                list((data if named else graph).query(query.split(" ORDER BY")[0]))
            )
            self.send_header(
                "Total",
                str(total + (1 if mode == "total" and len(received) > 1 else 0)),
            )
            self.send_header("Truncated", "true" if mode == "truncated" else "false")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    try:
        yield url + "/sparql", AcquisitionPolicy((url,)), received
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_complete_stable_pages_materialize_exact_rdf_for_offline_checks() -> None:
    """Positive actual multi-page extraction remains usable after endpoint shutdown."""
    with endpoint() as (url, policy, requests):
        result = acquire_endpoint(
            url,
            query=EndpointQuery(),
            policy=policy,
            completeness=CONTRACT,
            page_size=2,
        )
        assert result.report.verified and result.complete and result.consistent
        assert len(result.pages) == 3 and result.rows == 6 and result.source
        assert all(p["revision"] == ["immutable-a"] for p in requests)
        assert len({p["query"][0] for p in requests}) == 3
    graph = result.source.graph()
    assert {str(v) for v in graph.objects(None, P)} == {"00", "01", "02", "03", "04"}
    vocabulary = load_vocabulary(result.source)
    assert check_models(
        (ModelDeclaration("record", ("urn:Class",)),), vocabulary
    ).verified


def test_typed_parameters_and_named_graph_retain_exact_scope() -> None:
    """Typed binding escapes literal text and selects only the physical graph."""
    with endpoint(named=True) as (url, policy, requests):
        query = EndpointQuery(predicate=P, graph=URIRef("urn:G"))
        result = acquire_endpoint(
            url, query=query, policy=policy, completeness=CONTRACT, page_size=2
        )
        assert result.report.verified and result.rows == 5 and result.source
        assert all("GRAPH <urn:G>" in r["query"][0] for r in requests)
        assert all(t[1] == P for t in result.source.graph())
    malicious = Literal('" } SERVICE <http://example.invalid> { ?s ?p ?o } #')
    with endpoint() as (url, policy, requests):
        result = acquire_endpoint(
            url, query=EndpointQuery(object=malicious), policy=policy, page_size=2
        )
        assert result.rows == 0 and result.source is not None
        assert not result.report.verified and len(requests) == 1


def test_success_empty_page_and_undisclosed_caps_never_supply_completeness() -> None:
    """Successful short pages are useful local slices with unknown whole truth."""
    for mode in ("complete", "cap"):
        with endpoint(mode) as (url, policy, requests):
            result = acquire_endpoint(
                url, query=EndpointQuery(), policy=policy, page_size=2
            )
            assert (
                result.source is not None
                and not result.complete
                and not result.consistent
            )
            assert result.report.outcome == "unverified"
            assert len(requests) == (4 if mode == "complete" else 1)
    with endpoint("cap") as (url, policy, _):
        result = acquire_endpoint(
            url,
            query=EndpointQuery(),
            policy=policy,
            completeness=CONTRACT,
            page_size=2,
        )
        assert result.source is not None and not result.report.verified
        assert result.rows == 1


@pytest.mark.parametrize(
    "mode", ["change", "truncated", "wire", "outside", "duplicate", "total"]
)
def test_changed_snapshot_truncation_and_malformed_rows_cannot_pass(mode: str) -> None:
    """Server contradictions never certify the selected complete RDF slice."""
    with endpoint(mode) as (url, policy, _):
        result = acquire_endpoint(
            url,
            query=EndpointQuery(predicate=P),
            policy=policy,
            completeness=CONTRACT,
            page_size=2,
        )
        assert (
            not result.complete and not result.consistent and not result.report.verified
        )
        assert result.source is None


def test_capacity_blank_scopes_and_cancel_remain_truthful() -> None:
    """Independent blank labels and incomplete capacity receive no certificate."""
    with endpoint() as (url, policy, requests):
        result = acquire_endpoint(
            url,
            query=EndpointQuery(),
            policy=policy,
            completeness=CONTRACT,
            page_size=2,
            max_pages=1,
        )
        assert (
            result.rows == 2
            and result.source is not None
            and not result.report.verified
        )
        cancel = Event()
        cancel.set()
        result = acquire_endpoint(
            url, query=EndpointQuery(), policy=policy, cancel=cancel
        )
        assert result.report.outcome == "exhausted" and len(requests) == 1
    with endpoint("blank") as (url, policy, _):
        result = acquire_endpoint(
            url,
            query=EndpointQuery(),
            policy=policy,
            completeness=CONTRACT,
            page_size=1,
        )
        assert result.source is not None and not result.report.verified
        blanks = {s for s in result.source.graph().subjects() if isinstance(s, BNode)}
        assert len(blanks) == 2
        assert any(i.code == "endpoint.blank_scope_unverified" for i in result.report)
