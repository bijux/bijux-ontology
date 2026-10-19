"""Bounded host capture and logical finding ownership for native RDF streams."""

import json
import time
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from typing import Any, cast

from rdflib import BNode, Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.diagnostics.reports import issue_from_data as read_issue
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_input
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.stores.seals import verify_files
from bijux_ontology.rdf.stores.types import StoredRdf
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .protocol import read_statement, stored_data
from .types import GlobalCheck, StreamCheck, StreamFinding

_GLOBAL = ("shapes", "identity", "cardinality")


def _reserve(operation: Operation) -> tuple[int, dict[str, int]]:
    """Leave explicit host capacity for every bounded returned issue and witness."""
    capacity = min(
        (operation.remaining("work") - 4096) // 64,
        (operation.remaining("bytes") - 4096) // 64,
        (operation.remaining("states") - 256) // 4,
    )
    if capacity < 2048:
        raise LimitExceeded("RDF stream has insufficient bounded handoff allowance")
    reserves = {
        "work": 24 * capacity + 2048,
        "bytes": 32 * capacity + 2048,
        "states": 2 * capacity + 128,
    }
    return capacity, reserves


def check_rdf_stream(
    source: RdfSource | Path,
    vocabulary: Vocabulary | Graph | StoredRdf,
    *,
    format: str | None = None,
    vocabulary_scope: QueryScope | None = None,
    exclude: Sequence[str] = (),
    check_types: bool = True,
    check_literals: bool = True,
    required_global: Sequence[GlobalCheck] = (),
    max_stream_bytes: int | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> StreamCheck:
    """Check plain local NT/NQ streams without retaining a complete data graph.

    max_bytes bounds each physical record conservatively, before native parsing;
    max_stream_bytes independently caps observed source bytes and defaults to
    max_total_bytes. Syntax, escapes and term validity belong to PyOxigraph.
    Exact predicate/type/literal checks reuse existing validators. Duplicate
    statement occurrences stay distinct. Requested global work remains unknown.

    Worker work/bytes/states/triples are cumulative. Its bounded issue collection
    reserves at most the caller's remaining logical slots; reconstructed ordinary
    issues are charged once by the host. A declared output allowance leaves host
    adoption capacity before worker execution. An unreturned worker prefix after
    hard deadline/cancellation is unavailable, never fabricated as checked data.
    """
    operation = Operation(limits, budget, timeout, cancel)
    if type(check_types) is not bool or type(check_literals) is not bool:
        raise InputError("Stream check options must be booleans")
    maximum = (
        operation.limits.max_total_bytes
        if max_stream_bytes is None
        else max_stream_bytes
    )
    if type(maximum) is not int or maximum <= 0:
        raise InputError("RDF stream byte cap must be a positive integer")
    issues = IssueCollector(operation)
    parsing = IssueCollector(operation)
    global_issues = IssueCollector(operation)
    findings: list[StreamFinding] = []
    required: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    completed = False
    statements = selected = checks = count = 0
    digest = None
    document = effective = None
    try:
        values = capture_items(required_global, str, operation)
        for name in values:
            bound_text(name, operation)
            if name not in _GLOBAL:
                raise InputError("Unknown required global RDF stream obligation")
        required = values
        if len(set(required)) != len(required):
            raise InputError("Required global stream obligations must be unique")
        values = capture_items(exclude, str, operation)
        operation.consume("states", len(values))
        for iri in values:
            bound_text(iri, operation)
            absolute_iri(iri)
        excluded = values
        if not isinstance(source, RdfSource | Path):
            raise InputError(
                "RDF streaming requires an explicit local Path or RdfSource"
            )
        resource = source if isinstance(source, RdfSource) else RdfSource(source)
        if not isinstance(resource.content, Path) or resource.compression is not None:
            raise InputError("Stream profile supports plain local Path-backed NT/NQ")
        if resource.union:
            raise InputError(
                "Stream checks retain physical graphs, not flattened unions"
            )
        bound_text(str(resource.content), operation)
        path = resource.content.resolve()
        prepared = prepare_input(
            replace(resource, content=path), format=format, budget=operation
        )
        if prepared.syntax not in ("nt", "nquads"):
            raise InputError("Stream profile supports N-Triples or N-Quads only")
        document, effective = prepared.document_iri, prepared.effective_url
        stored = None
        declarations: list[list[object]] = []
        if isinstance(vocabulary, StoredRdf):
            if vocabulary_scope is None:
                raise InputError("Disk stream vocabulary requires explicit QueryScope")
            stored = stored_data(vocabulary, vocabulary_scope, operation)
            verify_files(vocabulary.directory, vocabulary.files, operation)
        else:
            if vocabulary_scope is not None:
                raise InputError("Only disk stream vocabulary accepts QueryScope")
            vocab = (
                vocabulary
                if isinstance(vocabulary, Vocabulary)
                else load_vocabulary(vocabulary, budget=operation)
            )
            index = vocab._kind_index(("local", "imports", "lookup"), operation)
            operation.consume("states", len(index))
            for iri, kinds in index.items():
                bound_text(iri, operation)
                operation.consume("states", len(kinds))
                for kind in kinds:
                    bound_text(kind, operation)
                declarations.append([iri, tuple(kinds)])
        request: dict[str, Any] = {
            "path": str(path),
            "syntax": prepared.syntax,
            "document_iri": document,
            "location": resource.location or str(path),
            "role": resource.role,
            "graph": resource.graph_name,
            "vocabulary": declarations,
            "stored": stored,
            "exclude": excluded,
            "check_types": check_types,
            "check_literals": check_literals,
            "max_stream_bytes": maximum,
            "limits": asdict(operation.limits),
        }
        operation.consume("bytes", json_bound(request, operation))
        envelope = {
            "output_cap": operation.limits.max_total_bytes,
            "allowances": {kind: operation.remaining(kind) for kind in BUDGET_KINDS},
        }
        operation.consume("bytes", json_bound(envelope, operation))
        output_cap, reserves = _reserve(operation)
        allowances = {
            kind: operation.remaining(kind) - reserves.get(kind, 0)
            for kind in BUDGET_KINDS
        }
        request.update(output_cap=output_cap, allowances=allowances)
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        raw = execute_worker(
            "bijux_ontology.data.streams.worker",
            payload,
            cap=max(len(payload), output_cap),
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        if isinstance(vocabulary, StoredRdf):
            verify_files(vocabulary.directory, vocabulary.files, operation)
        if len(raw) > output_cap:
            raise ResourceError("RDF stream worker exceeded its output reservation")
        response = object_value(
            decode_json(
                raw, max_bytes=output_cap, max_depth=operation.limits.max_depth
            ),
            location="RDF stream worker",
        )
        if response.get("status") != "result":
            raise LimitExceeded("RDF stream worker could not complete its protocol")
        consumed = response.get("consumed")
        if not isinstance(consumed, dict) or set(consumed) != set(BUDGET_KINDS):
            raise ResourceError("RDF stream worker omitted cumulative counters")
        for kind in BUDGET_KINDS:
            amount = consumed[kind]
            if type(amount) is not int or not 0 <= amount <= allowances[kind]:
                raise ResourceError("Invalid RDF stream worker counters")
            if kind != "issues":
                operation.consume(kind, amount)
        operation.consume("bytes", json_bound(response, operation))
        for key in ("statements", "selected", "checks", "bytes_read"):
            if type(response.get(key)) is not int or cast(int, response[key]) < 0:
                raise ResourceError("Invalid stream completion counts")
        statements, selected, checks, count = (
            cast(int, response[k])
            for k in ("statements", "selected", "checks", "bytes_read")
        )
        if (
            statements > operation.limits.max_triples
            or selected > statements
            or checks > 3 * selected
            or count > maximum + 1
        ):
            raise ResourceError("Stream completion counts exceed selected scope")
        complete_value = response.get("complete")
        if type(complete_value) is not bool:
            raise ResourceError("Stream completion must be a boolean")
        digest = response.get("sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ResourceError("Invalid observed stream digest")
        blanks: dict[str, BNode] = {}
        records, errors = response.get("records"), response.get("parse_issues")
        if (
            not isinstance(records, list)
            or not isinstance(errors, list)
            or len(records) + len(errors) > allowances["issues"]
        ):
            raise ResourceError("Stream findings exceed reserved logical issue slots")
        operation.consume("states", len(records) + len(errors))
        for record in records:
            data = object_value(record, location="RDF stream worker")
            issue = read_issue(data.get("issue"))
            statement = read_statement(data.get("statement"), blanks, operation)
            if statement.index > statements:
                raise ResourceError("Stream witness is outside parsed prefix")
            issues.append(issue)
            findings.append(StreamFinding(statement, issue))
        for record in errors:
            parsing.append(
                read_issue(
                    object_value(record, location="RDF stream worker").get("issue")
                )
            )
        stopped = response.get("stopped")
        if not isinstance(stopped, str):
            raise ResourceError("Invalid stream stop evidence")
        bound_text(stopped, operation)
        if stopped:
            issues.exhausted(stopped, location="data.stream")
            parsing.exhausted(stopped, location="data.stream.parse")
        completed = complete_value and not stopped and not errors
    except LimitExceeded as error:
        issues.exhausted(str(error), location="data.stream")
        parsing.exhausted(str(error), location="data.stream.parse")
    except (InputError, OSError) as error:
        try:
            issue = Issue("data.stream_unverified", str(error), kind="unverified")
            issues.append(issue)
            parsing.append(issue)
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="data.stream")
    if not checks and completed:
        try:
            issues.append(
                Issue(
                    "data.stream_unverified",
                    "No selected statement-local comparisons completed",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="data.stream")
    global_report = None
    if required:
        try:
            for name in required:
                global_issues.append(
                    Issue(
                        "data.stream_global_unverified",
                        "Statement checks do not execute global obligations",
                        expected=name,
                        kind="unverified",
                    )
                )
        except LimitExceeded as error:
            global_issues.exhausted(str(error), location="data.stream.global")
        global_report = Report(
            "data.stream.global",
            issues=tuple(global_issues),
            requested=len(required),
            complete=False,
        )
    parse_report = Report(
        "data.stream.parse",
        int(completed),
        tuple(parsing),
        requested=1,
        complete=completed,
    )
    local_report = Report(
        "data.stream.local",
        checks,
        tuple(issues),
        requested=max(1, checks),
        complete=completed,
        families=(
            "vocabulary-usage",
            *(("literal-samples",) if check_literals else ()),
        ),
        exclusions=(*_GLOBAL, *excluded),
        regime="statement-local-rdf-occurrences",
    )
    children = (parse_report, local_report) + (
        (global_report,) if global_report is not None else ()
    )
    report = Report(
        "data.stream",
        int(completed),
        requested=1,
        complete=completed,
        children=children,
        regime="statement-local-rdf-occurrences",
    )
    return StreamCheck(
        document,
        effective,
        statements,
        selected,
        count,
        cast(str | None, digest),
        tuple(findings),
        parse_report,
        local_report,
        global_report,
        report,
    )
