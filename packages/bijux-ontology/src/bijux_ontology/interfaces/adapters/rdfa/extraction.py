"""Bounded native HTML5 RDFa with document association and no implicit union."""

import json
import math
import threading
import time
from importlib.resources import as_file, files
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, URIRef
from rdflib.term import Node

from bijux_ontology._json import _measure, decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.limits import Budget, BudgetKind, IssueCollector, Limits, bound_text
from bijux_ontology.processes.transport import checkpoint, exchange_worker
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import literal_term, validate_triple

from .types import RdfaExtraction, RdfaProducer, RdfaRuntime

_KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "states",
    "work",
    "triples",
    "resources",
    "issues",
)
_KEYS = {
    "protocol",
    "status",
    "producer",
    "consumed",
    "quads",
    "diagnostics",
    "unsupportedCopy",
}


def _object(value: object) -> dict[str, object]:
    """Reject protocol coercion and unknown container shapes."""
    if not isinstance(value, dict):
        raise ResourceError("Native RDFa returned an invalid protocol object")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    """Keep repeated producer observations finite and explicit."""
    if not isinstance(value, list):
        raise ResourceError("Native RDFa returned an invalid protocol array")
    return value


def _text(value: object, budget: Budget) -> str:
    """Qualify text before term construction or diagnostic attachment."""
    if type(value) is not str:
        raise ResourceError("Native RDFa returned nontext term metadata")
    bound_text(value, budget)
    return value


def _term(value: object, blanks: dict[str, BNode], budget: Budget) -> Node:
    """Retain exact RDFJS lexical terms and shared document-local blank identity."""
    parts = _array(value)
    if len(parts) not in (2, 4):
        raise ResourceError("Native RDFa returned an unsupported RDFJS term")
    kind, text = parts[0], _text(parts[1], budget)
    budget.consume("states")
    budget.consume("bytes", 64)
    if kind == "NamedNode" and len(parts) == 2:
        absolute_iri(text)
        return URIRef(text)
    if kind == "BlankNode" and len(parts) == 2 and text:
        if text not in blanks:
            blanks[text] = BNode()
        return blanks[text]
    if kind == "Literal" and len(parts) == 4:
        language, datatype = _text(parts[2], budget), _text(parts[3], budget)
        return literal_term(text, language=language or None, datatype=datatype)
    raise ResourceError("Native RDFa returned an unsupported RDFJS term")


def _run(
    text: str,
    document_url: str,
    runtime: RdfaRuntime,
    budget: Budget,
    deadline: float,
    cancel: threading.Event | None,
) -> dict[str, object]:
    """Reserve host decoding before dispatching the native process allowance."""
    bound_text(runtime.executable, budget)
    directory = str(runtime.directory.resolve())
    bound_text(directory, budget)
    request: dict[str, object] = {
        "protocol": 1,
        "host": "html5",
        "text": text,
        "document_url": document_url,
        "max_depth": budget.limits.max_depth,
        "output_cap": 1,
        "allowances": dict.fromkeys(_KINDS, 0),
    }
    size = _measure(request, budget)
    budget.consume("bytes", 24 * size + 8192)
    cap = min(
        budget.limits.max_bytes,
        budget.remaining("bytes") // 64,
        budget.remaining("work") // 8,
    )
    if cap < 1024:
        raise LimitExceeded("RDFa reply has no reserved adoption allowance")
    allowances = {key: budget.remaining(key) // 4 for key in _KINDS}
    request["allowances"] = allowances
    request["output_cap"] = cap
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    checkpoint(deadline, cancel)
    dispatched = received = False
    try:
        with as_file(files(__package__).joinpath("worker.cjs")) as worker:
            dispatched = True
            raw = exchange_worker(
                [
                    runtime.executable,
                    str(worker),
                    str(max(cap, len(payload))),
                    directory,
                ],
                payload,
                cap=max(cap, len(payload)),
                timeout=max(0.000001, deadline - time.monotonic()),
                cancel=cancel,
            )
        checkpoint(deadline, cancel)
        if len(raw) > cap:
            raise ResourceError("Native RDFa exceeded its reserved response cap")
        # JSON scalars and vectors are reserved before the maintained decoder allocates.
        budget.consume("bytes", 32 * len(raw) + 1024)
        budget.consume("work", len(raw) + 1)
        response = _object(decode_json(raw, max_bytes=cap, max_depth=16))
        if (
            set(response) != _KEYS
            or type(response["protocol"]) is not int
            or response["protocol"] != 1
        ):
            raise ResourceError("Native RDFa returned an unsupported protocol")
        if (
            response["status"]
            not in ("result", "exhausted", "unavailable", "input_error")
            or type(response["unsupportedCopy"]) is not bool
            or not isinstance(response["quads"], list)
            or not isinstance(response["diagnostics"], list)
        ):
            raise ResourceError("Native RDFa returned an invalid observation receipt")
        consumed = _object(response["consumed"])
        if set(consumed) != set(_KINDS):
            raise ResourceError("Native RDFa omitted its cumulative counters")
        for key in _KINDS:
            count = consumed[key]
            if type(count) is not int or not 0 <= count <= allowances[key]:
                raise ResourceError(
                    "Native RDFa returned an invalid cumulative counter"
                )
        for key in _KINDS:
            budget.consume(key, cast(int, consumed[key]))
        received = True
        return response
    finally:
        if dispatched and not received:
            # Missing native receipts cannot restore the allowance delegated to
            # a crashed, cancelled or malformed producer on a shared Budget.
            for key in _KINDS:
                budget.consume(key, allowances[key])


def extract_rdfa(
    content: str | bytes,
    document_url: str,
    *,
    runtime: RdfaRuntime | None = None,
    host: Choice["html5", "xhtml5"] = "html5",
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> RdfaExtraction:
    """Extract supplied HTML5 into owned asserted RDF through optional Node.

    The native HTML base, language, prefix, reverse and list rules apply. XHTML5
    and unresolved RDFa copy expansion remain unverified. No URLs are fetched.
    Association precision is document-only: the producer supplies neither term
    origins nor source spans. JSON-LD scripts are not extracted or unioned here.
    Returned RDF uses ordinary emission, vocabulary and selected-field checks.
    """
    if type(content) not in (str, bytes) or type(document_url) is not str:
        raise InputError("RDFa requires original UTF-8 text/bytes and a document URL")
    if runtime is not None and not isinstance(runtime, RdfaRuntime):
        raise InputError("RDFa runtime requires RdfaRuntime")
    if host not in ("html5", "xhtml5"):
        raise InputError("Select an explicit RDFa host: html5 or xhtml5")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise InputError("RDFa timeout must be finite and positive")
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("RDFa cancellation requires threading.Event")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("RDFa limits require Limits")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    diagnostics: list[str] = []
    producer = None
    source = None
    complete = False
    deadline = time.monotonic() + timeout
    try:
        bound_text(document_url, current)
        absolute_iri(document_url)
        checkpoint(deadline, cancel)
        if host != "html5":
            raise ResourceError(
                "The maintained RDFa profile qualifies HTML5 only; XHTML5 is unverified"
            )
        if runtime is None:
            raise ResourceError(
                "RDFa requires explicit Node and a runtime prepared "
                "with the shipped npm lock"
            )
        if len(content) > current.limits.max_bytes:
            raise LimitExceeded("HTML exceeds max_bytes")
        if isinstance(content, bytes):
            current.consume("bytes", 4 * len(content))
            current.consume("work", len(content) + 1)
            text = content.decode("utf-8")
        else:
            text = content
        bound_text(text, current)
        response = _run(text, document_url, runtime, current, deadline, cancel)
        status = response["status"]
        if status not in ("result", "exhausted", "unavailable", "input_error"):
            raise ResourceError("Native RDFa returned an invalid status")
        raw_producer = response["producer"]
        if raw_producer is not None:
            native = _object(raw_producer)
            if (
                set(native) != {"node", "parser", "host"}
                or native["parser"] != "3.0.2"
                or native["host"] != "html5"
            ):
                raise ResourceError("Native RDFa producer identity is unsupported")
            node = _text(native["node"], current)
            if not node.startswith("v"):
                raise ResourceError("Native RDFa omitted its actual Node version")
            producer = RdfaProducer(node, "3.0.2")
        for value in _array(response["diagnostics"]):
            diagnostics.append(_text(value, current))
        if type(response["unsupportedCopy"]) is not bool:
            raise ResourceError("Native RDFa omitted expansion qualification")
        if response["unsupportedCopy"]:
            issues.append(
                Issue(
                    "rdfa.expansion_unverified",
                    "Unresolved RDFa copy expansion requires "
                    "unavailable controlled resources",
                    document_url,
                    kind="unverified",
                )
            )
        if status == "exhausted":
            raise LimitExceeded("Native RDFa exceeded its cumulative allowance")
        if status == "unavailable":
            raise ResourceError(
                "RDFa requires npm ci with the shipped rdfa-streaming-parser 3.0.2 lock"
            )
        if status == "input_error":
            raise InputError(
                "Maintained RDFa extraction did not complete; see native diagnostics"
            )
        if producer is None:
            raise ResourceError("Native RDFa omitted producer identity")
        graph = Graph(bind_namespaces="none")
        blanks: dict[str, BNode] = {}
        for row in _array(response["quads"]):
            checkpoint(deadline, cancel)
            parts = _array(row)
            current.consume("work")
            current.consume("states")
            current.consume("bytes", 192)
            if len(parts) != 4 or parts[3] != ["DefaultGraph", ""]:
                raise ResourceError("HTML5 RDFa returned an unsupported graph role")
            subject, predicate, value = (
                _term(part, blanks, current) for part in parts[:3]
            )
            validate_triple(subject, predicate, value)
            graph.add((subject, predicate, value))
        if not graph:
            issues.append(
                Issue(
                    "rdfa.empty_extraction",
                    "No actual RDFa assertions establish the requested extraction",
                    document_url,
                    kind="unverified",
                )
            )
        if not issues:
            candidate = prepare_rdf(
                RdfSource(graph, document_iri=document_url), budget=current
            )
            checkpoint(deadline, cancel)
            source = candidate
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location=document_url)
    except (InputError, UnicodeError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "rdfa.extraction_unverified",
                    str(error),
                    document_url,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location=document_url)
    return RdfaExtraction(
        document_url,
        source,
        producer,
        Report(
            "rdfa.extraction",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="asserted-html5-rdfa",
        ),
        diagnostics=tuple(diagnostics),
    )
