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
from bijux_ontology.execution.allowances import DelegatedAllowance
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.loading import prepare_input
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.stores.types import StoredRdf
from bijux_ontology.resources.files import verify_files
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .protocol import read_completion, read_statement, stored_data
from .types import GlobalCheck, StreamCheck, StreamFinding

_GLOBAL = ("shapes", "identity", "cardinality")


def _reserve(
    operation: Operation,
) -> tuple[int, dict[BudgetKind, int], dict[BudgetKind, int]]:
    """Separate finite decoding and typed-adoption maxima before native admission."""
    capacity = min(
        (operation.remaining("work") - 4096) // 64,
        (operation.remaining("bytes") - 8192) // 512,
        (operation.remaining("states") - 256) // 4,
    )
    if capacity < 2048:
        raise LimitExceeded("RDF stream has insufficient bounded handoff allowance")
    decoder = dict.fromkeys(BUDGET_KINDS, 0)
    decoder.update(
        {
            "work": 4 * capacity + 1024,
            "bytes": 128 * capacity + 4096,
            "states": capacity + 64,
        }
    )
    adoption = dict.fromkeys(BUDGET_KINDS, 0)
    adoption.update(
        {
            "work": 20 * capacity + 1024,
            "bytes": 128 * capacity + 4096,
            "states": capacity + 128,
            "issues": operation.remaining("issues"),
        }
    )
    return capacity, decoder, adoption


def _retain_failure(
    error: InputError | OSError, issues: IssueCollector, parsing: IssueCollector
) -> None:
    """Retain one paid stream failure identity or its bounded exhaustion markers."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error), location="data.stream")
        parsing.exhausted(str(error), location="data.stream.parse")
        return
    try:
        issue = Issue("data.stream_unverified", str(error), kind="unverified")
        issues.append(issue)
        parsing.append(issue)
    except LimitExceeded as stopped:
        issues.exhausted(str(stopped), location="data.stream")
        parsing.exhausted(str(stopped), location="data.stream.parse")


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
    adoption: ControlBudget | None = None
    adoption_paid_issues = 0
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
        # These fixed protocol owners are funded before their disjoint grant capture.
        operation.consume("states", 24)
        operation.consume("bytes", 8192)
        output_cap, decoder_maxima, adoption_maxima = _reserve(operation)
        native_grants = {
            kind: operation.remaining(kind)
            - decoder_maxima[kind]
            - adoption_maxima[kind]
            for kind in BUDGET_KINDS
        }
        delegated = DelegatedAllowance(operation, native_grants)
        allowances = delegated.to_data()
        # Native issue counts bound output; reconstructed host Issues alone pay.
        allowances["issues"] = operation.remaining("issues")
        decoder = ControlBudget(
            replace(operation.limits, max_bytes=output_cap), decoder_maxima
        )
        decoder_costs = DelegatedAllowance(operation, decoder_maxima)
        request.update(output_cap=output_cap, allowances=allowances)
        payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode()
        dispatched = received = False
        try:
            operation.consume("work", 0)
            dispatched = True
            raw = execute_worker(
                "bijux_ontology.data.streams.worker",
                payload,
                cap=max(len(payload), output_cap),
                timeout=max(0.000001, operation.deadline - time.monotonic()),
                cancel=operation.cancel,
            )
            if len(raw) > output_cap:
                raise ResourceError("RDF stream worker exceeded its output reservation")
            decoder.consume("bytes", 8 * len(raw) + 1024)
            decoder.consume("work", len(raw) + 1)
            response, consumed = read_completion(
                decode_json(
                    raw,
                    max_bytes=output_cap,
                    max_depth=operation.limits.max_depth,
                    budget=decoder,
                ),
                limits=operation.limits,
                allowances=allowances,
                maximum=maximum,
                budget=decoder,
            )
            projected = dict(consumed)
            projected["issues"] = 0
            delegated.settle(projected)
            received = True
        finally:
            try:
                decoder_costs.settle(decoder.consumed())
            finally:
                if dispatched and not received:
                    delegated.forfeit()
        if isinstance(vocabulary, StoredRdf):
            # This is fresh external input verification, never stopped receipt decoding.
            verify_files(vocabulary.directory, vocabulary.files, operation)
        # Fresh verification can shrink terminal maxima; it cannot enlarge them.
        adopted = {
            kind: min(adoption_maxima[kind], operation.remaining(kind))
            for kind in BUDGET_KINDS
        }
        adoption = ControlBudget(operation.limits, adopted)
        adoption_costs = DelegatedAllowance(operation, adopted)
        # These empty collectors now own only bounded completed terminal data.
        issues.budget = adoption
        parsing.budget = adoption
        rejected = False
        try:
            adoption.consume("states", 16)
            adoption.consume("bytes", 1024)
            adoption.consume("work", 16)
            statements, selected, checks, count = (
                cast(int, response[k])
                for k in ("statements", "selected", "checks", "bytes_read")
            )
            digest = response["sha256"]
            blanks: dict[str, BNode] = {}
            records, errors = response["records"], response["parse_issues"]
            for record in records:
                data = object_value(record, location="RDF stream worker")
                adoption.consume("work", 4)
                adoption.consume("states", 8)
                adoption.consume("bytes", 512 + 4 * json_bound(data, adoption))
                issue = read_issue(data.get("issue"))
                statement = read_statement(data.get("statement"), blanks, adoption)
                if statement.index > statements:
                    raise ResourceError("Stream witness is outside parsed prefix")
                issues.append(issue)
                findings.append(StreamFinding(statement, issue))
            for record in errors:
                data = object_value(record, location="RDF stream worker")
                adoption.consume("work", 4)
                adoption.consume("states", 8)
                adoption.consume("bytes", 512 + 4 * json_bound(data, adoption))
                parsing.append(read_issue(data.get("issue")))
            stopped = response["stopped"]
            bound_text(stopped, adoption)
            if stopped:
                issues.exhausted(stopped, location="data.stream")
                parsing.exhausted(stopped, location="data.stream.parse")
            completed = response["complete"] and not stopped and not errors
            if not checks and completed:
                adoption.consume("states", 8)
                adoption.consume("bytes", 1024)
                adoption.consume("work", 4)
                issues.append(
                    Issue(
                        "data.stream_unverified",
                        "No selected statement-local comparisons completed",
                        kind="unverified",
                    )
                )
        except (InputError, OSError) as error:
            completed = False
            _retain_failure(error, issues, parsing)
            rejected = True
        finally:
            adoption_paid_issues = adoption.consumed()["issues"]
            adoption_costs.settle(adoption.consumed())
            if rejected:
                # Known native costs were paid; rejected output forfeits only
                # unused native capacity after its performed host costs settle.
                delegated.forfeit()
        # Completed observations do not renew permission for subsequent semantic work.
        operation.consume("work", 0)
    except (InputError, OSError) as error:
        completed = False
        _retain_failure(error, issues, parsing)
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
    if adoption is not None:
        references = 16 + 4 * (len(issues) + len(parsing))
        operation._register_transferred_issues(
            tuple(
                issue
                for collector in (issues, parsing)
                for issue in collector
                if issue.code != "execution.limit_exceeded" or issue.kind != "exhausted"
            ),
            paid_issues=adoption_paid_issues,
            max_references=references,
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
