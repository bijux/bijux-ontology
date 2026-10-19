"""Markup value semantics are opt-in, bounded and separate from original RDF."""

import importlib.util
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import FrameType
from typing import Any
from typing import Literal as EventKind

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, Limits
from bijux_ontology.execution.processes import transport
from bijux_ontology.execution.processes.ownership import spawn_owned_process
from bijux_ontology.execution.receipts import TerminalResponse
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.literal_values.markup import (
    MarkupCanonicalization,
    compare_markup_values,
    inspect_markup_literal,
)
from bijux_ontology.rdf.terms import term_key

CANON: MarkupCanonicalization = "xml-exc-c14n-with-comments"


def _xml(text: str) -> Literal:
    """Never normalize the original source RDF lexical spelling."""
    return Literal(text, datatype=RDF.XMLLiteral, normalize=False)


def test_actual_xml_canonical_values_do_not_change_rdf_term_identity() -> None:
    left, right = _xml('<p b="2" a="1"/>'), _xml('<p a="1" b="2"></p>')
    result = compare_markup_values(left, right, regime=CANON)
    assert term_key(left) != term_key(right)
    if importlib.util.find_spec("lxml") is None:
        assert result.report.outcome == "unverified"
        assert result.before is not None
        assert "[markup]" in result.before.report.issues[0].message
        return
    assert result.report.verified and result.relation == "equal"
    assert result.before is not None and result.before.original is left
    assert result.after is not None and result.after.original is right
    assert result.before.canonical == b'<p a="1" b="2"></p>'
    for before, after in (
        ('<p xml:lang="en"/>', '<p xml:lang="fr"/>'),
        ('<p xml:base="https://a.example/"/>', '<p xml:base="https://b.example/"/>'),
        ("<!--one--><p/>", "<!--two--><p/>"),
        ("<?p one?><p/>", "<?p two?><p/>"),
        ('<p xmlns="urn:a"/>', '<p xmlns="urn:b"/>'),
        ('<a:p xmlns:a="urn:x"/>', '<b:p xmlns:b="urn:x"/>'),
    ):
        changed = compare_markup_values(_xml(before), _xml(after), regime=CANON)
        assert changed.relation == "different"
        assert changed.report.outcome == "disagreement"


def test_xml_mixed_fragments_empty_values_and_honest_html_inspection() -> None:
    pytest.importorskip("lxml")
    original = _xml(
        'lead<a xmlns="urn:a" xml:lang="en" xml:base="https://example.org/">'
        "x<b>y</b>z</a>tail<!--keep--><?p q?>"
    )
    graph = Graph().add((URIRef("urn:s"), URIRef("urn:p"), original))
    result = inspect_markup_literal(original, canonicalization=CANON)
    assert result.report.verified and result.text == "leadxyztail"
    assert result.element_names == ("{urn:a}a", "{urn:a}b")
    assert result.comment_count == 1 and result.canonical is not None
    assert b'xml:lang="en"' in result.canonical
    assert b'xml:base="https://example.org/"' in result.canonical
    assert b"<!--keep--><?p q?>" in result.canonical
    assert len(graph) == 1 and next(graph.objects()) is original
    empty = inspect_markup_literal(_xml(""), canonicalization=CANON)
    assert empty.report.verified and empty.canonical == b""
    html = Literal(
        '<P lang="en">A<br>B<!--keep--></P>', datatype=RDF.HTML, normalize=False
    )
    inspected = inspect_markup_literal(html)
    assert inspected.report.verified and inspected.kind == "html"
    assert inspected.text == "AB" and inspected.element_names == ("p", "br")
    assert inspected.original is html and inspected.comment_count == 1
    assert inspected.canonical is None
    assert inspected.report.regime == "lxml-html-fragment-inspection"
    assert inspect_markup_literal(Literal("", datatype=RDF.HTML)).report.verified
    unsupported = compare_markup_values(html, html, regime=CANON)
    assert unsupported.relation == "indeterminate"
    assert unsupported.report.outcome == "unverified"


def test_malformed_forbidden_resources_and_unsupported_canonical_conditions() -> None:
    pytest.importorskip("lxml")
    for text in ("<a>", "<p:x/>", "one &unknown;", '<?xml version="1.0"?><a/>'):
        result = inspect_markup_literal(_xml(text), canonicalization=CANON)
        assert result.report.outcome == "input_error" and result.canonical is None
    unknown = inspect_markup_literal(
        _xml('<a xmlns="relative"/>'), canonicalization=CANON
    )
    assert unknown.report.outcome == "unverified" and unknown.canonical is None
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        """Count real attempted retrieval rather than replacing native parsers."""

        def do_GET(self) -> None:
            """Respond if an unexpected parser lookup reaches the loopback server."""
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'<!ENTITY ext "unwanted">')

        def log_message(self, *args: object) -> None:
            """Keep expected test diagnostics in the assertions."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/external.dtd"
        external = inspect_markup_literal(
            _xml(f'<!DOCTYPE x SYSTEM "{url}"><x>&ext;</x>'), canonicalization=CANON
        )
        assert external.report.outcome == "unverified" and external.canonical is None
        internal = inspect_markup_literal(
            _xml('<!DOCTYPE x [<!ENTITY a "value">]><x>&a;</x>')
        )
        assert internal.report.outcome == "unverified"
        assert not calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_actual_native_depth_output_and_cumulative_control_bounds() -> None:
    pytest.importorskip("lxml")
    for datatype in (RDF.XMLLiteral, RDF.HTML):
        deep = Literal(
            "<div>" * 300 + "x" + "</div>" * 300, datatype=datatype, normalize=False
        )
        result = inspect_markup_literal(deep)
        assert result.report.outcome == "exhausted"
        assert result.canonical is None and not result.element_names
    amplified = _xml('<a xmlns:p="urn:' + "x" * 150 + '">' + "<p:b/>" * 10 + "</a>")
    result = inspect_markup_literal(
        amplified, canonicalization=CANON, limits=Limits(max_bytes=1000)
    )
    assert result.report.outcome == "exhausted" and result.canonical is None
    huge = inspect_markup_literal(_xml("é" * 1000), limits=Limits(max_bytes=1000))
    assert huge.report.outcome == "exhausted" and huge.original is None
    shared = Budget(Limits(max_resources=1))
    first = inspect_markup_literal(_xml("<a/>"), budget=shared)
    second = inspect_markup_literal(_xml("<a/>"), budget=shared)
    assert first.report.verified and second.report.outcome == "exhausted"
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    invalid = inspect_markup_literal(_xml("<a>"), budget=spent)
    assert invalid.report.outcome == "exhausted"
    assert inspect_markup_literal(_xml("<a/>")).report.verified


def test_live_owned_worker_cancellation_reaps_and_fresh_operation_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("lxml")
    cancel = Event()
    processes: list[Any] = []
    original = spawn_owned_process

    def spawn(*args: Any, **kwargs: Any) -> Any:
        """Cancel only after a real child process was successfully launched."""
        process = original(*args, **kwargs)
        processes.append(process)
        assert process.poll() is None
        cancel.set()
        return process

    monkeypatch.setattr(transport, "spawn_owned_process", spawn)
    stopped = inspect_markup_literal(_xml("<a/>"), cancel=cancel)
    assert stopped.report.outcome == "exhausted"
    assert processes and all(process.poll() is not None for process in processes)
    monkeypatch.setattr(transport, "spawn_owned_process", original)
    recovered = inspect_markup_literal(_xml("<a/>"), canonicalization=CANON)
    assert recovered.report.verified


@pytest.mark.parametrize("phase", ("completed", "interrupted", "killed"))
def test_inherited_cancellation_preserves_terminal_costs_and_reaps_native_work(
    phase: str,
) -> None:
    """Observe real receipts and children without replacing any native results."""
    pytest.importorskip("lxml")
    source = _xml("<p>value</p>")
    ledger = Budget()
    cancellation = Event()
    operation = Operation(None, ledger, 30, cancellation)
    processes: list[subprocess.Popen[bytes]] = []
    grants: dict[str, int] = {}
    native: dict[str, int] = {}
    host: dict[str, int] = {}
    at_dispatch: dict[str, int] = {}
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        value: object,
    ) -> None:
        """Set the inherited Event only at an actual native lifetime boundary."""
        if previous is not None:
            previous(frame, event, value)
        is_worker = (
            frame.f_code is execute_worker.__code__
            and frame.f_locals.get("module")
            == "bijux_ontology.rdf.literal_values.markup.worker"
        )
        if is_worker and event == "call":
            grants.update(json.loads(frame.f_locals["payload"])["allowances"])
            at_dispatch.update((kind, ledger.remaining(kind)) for kind in BUDGET_KINDS)
        if event != "return":
            return
        process = frame.f_locals.get("self")
        if (
            frame.f_code is subprocess.Popen.__init__.__code__
            and isinstance(process, subprocess.Popen)
            and isinstance(process.args, list)
            and "bijux_ontology.rdf.literal_values.markup.worker" in process.args
        ):
            processes.append(process)
            if phase == "interrupted":
                cancellation.set()
            elif phase == "killed":
                process.kill()
        if is_worker and isinstance(value, bytes):
            native.update(json.loads(value)["consumed"])
            if phase == "completed":
                cancellation.set()
        if frame.f_code is TerminalResponse.close.__code__:
            host.update(frame.f_locals["self"]._host.consumed())

    sys.setprofile(observe)
    try:
        stopped = inspect_markup_literal(
            source, canonicalization=CANON, budget=operation
        )
    finally:
        sys.setprofile(previous)
    assert stopped.report.outcome == (
        "unverified" if phase == "killed" else "exhausted"
    )
    assert processes and all(process.returncode is not None for process in processes)
    assert bool(native) == (phase == "completed")
    expected = native if native else grants
    assert expected and host
    if phase == "killed":
        # A real transport failure needs the host's own Issue allowance.
        assert ledger.remaining("issues") == ledger.limits.max_issues - 1
        assert any(issue.kind == "unverified" for issue in stopped.report.issues)
    else:
        assert {
            kind: at_dispatch[kind] - ledger.remaining(kind) for kind in BUDGET_KINDS
        } == {kind: expected[kind] + host[kind] for kind in BUDGET_KINDS}
    assert inspect_markup_literal(source, canonicalization=CANON).report.verified
