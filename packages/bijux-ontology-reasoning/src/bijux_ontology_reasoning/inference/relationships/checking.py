"""Bounded actual named relationship enumeration over one qualified OWL closure."""

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Report

from ...axioms.types import NamedEntity
from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import (
    result_boolean,
    result_counter,
    result_records,
    result_text,
)
from ...backend.owlapi.receipts import qualify_reasoning_result, reasoning_header
from ...backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ...documents.sources.native import stage_native_documents
from ...documents.sources.rdf import stage_documents
from ...documents.types import OwlDocument
from .codec import _groups, _query
from .types import (
    InferredAnswer,
    InferredContracts,
    InferredQuery,
    RelationshipGroup,
)


def query_inferred_contracts(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    queries: Sequence[InferredQuery],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    max_results: int = 10_000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> InferredContracts:
    """Enumerate supported native named relations under exact declared query depth.

    Source qualification and consistency precede every answer. Retained groups
    carry equivalence membership and direct structural assertion owners; absence
    of an assertion does not prove a negation. Result caps and host exhaustion
    retain completed prefixes while required enumeration remains incomplete.
    No graph is materialized or mutated. Class satisfiability is independent of
    whether its relationship enumeration completed correctly.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    requests: tuple[InferredQuery, ...] = ()
    requested_count = 0
    answers: list[InferredAnswer | None] = []
    children: list[Report] = []
    originals: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    consistent = raw_consistent = in_profile = None
    native_error = None
    reasoner = capability = None
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    finished = False
    try:
        if type(max_results) is not int or max_results <= 0:
            raise InputError("max_results requires a positive finite integer")
        selected = capture_items(queries, InferredQuery, operation)
        requested_count = len(selected)
        if not selected:
            raise InputError("No inferred relationship queries were supplied")
        operation.consume("states", len(selected) * 4)
        operation.consume("bytes", 512 * len(selected))
        wire = [_query(query, operation) for query in selected]
        # No raw request metadata enters the result before every scalar is paid.
        requests = selected
        answers = [None] * len(requests)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="relationships-", dir=backend.artifacts) as job:
            directory = Path(job)
            if isinstance(source, OwlDocument):
                originals, request = stage_native_documents(
                    source,
                    cast(Mapping[str, OwlDocument] | None, imports),
                    directory,
                    operation,
                )
                kind = "native"
            elif isinstance(source, PreparedSource):
                originals, request, _ = stage_documents(
                    source,
                    cast(Mapping[str, RdfSource | Payload] | None, imports),
                    directory,
                    operation,
                    issues,
                    children,
                )
                kind = "rdf"
            else:
                raise InputError(
                    "Inferred contracts require an owned RDF/native source"
                )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded("Inferred contracts have no result allowance")
            request.update(
                operation="relationships",
                input_kind=kind,
                queries=wire,
                max_results=min(max_results, operation.remaining("states")),
                max_nodes=operation.remaining("states"),
                max_issues=maximum,
                max_bytes=operation.limits.max_bytes,
                max_resources=operation.limits.max_resources,
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            data = invoke_worker(backend, request, directory, operation, heap_mb)
            capability = qualify_capability(data, backend, operation)
            children.append(capability.report)
            header = reasoning_header(data, operation)
            raw_consistent, in_profile, native_error = (
                header.raw_consistent,
                header.in_profile,
                header.native_error,
            )
            receipt = qualify_reasoning_result(
                data,
                originals,
                kind,
                operation,
                issues,
                header=header,
                scope="relationships",
            )
            raw_consistent, in_profile, native_error = (
                receipt.raw_consistent,
                receipt.in_profile,
                receipt.native_error,
            )
            loaders, reasoner = receipt.loaders, receipt.reasoner
            if not receipt.qualified:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "DL native input qualification did not complete"
                    )
                raise ResourceError("The selected DL reasoning scope is unqualified")
            if raw_consistent is not None:
                consistent = raw_consistent
                operation.consume("bytes", 256)
                operation.consume("states", 2)
                finding = (
                    None
                    if consistent
                    else Issue(
                        "owl.inconsistent", "The selected ontology is inconsistent"
                    )
                )
                if finding is not None:
                    issues.append(finding)
                consistency_report = Report(
                    "owl.consistency",
                    1,
                    () if finding is None else (finding,),
                    requested=1,
                )
                children.append(consistency_report)
            if consistent is not True:
                issues.append(
                    Issue(
                        "owl.relationship_consistency_unverified",
                        "An ordinary inferred view requires a consistent ontology",
                        kind="unverified",
                    )
                )
            else:
                seen = set()
                for raw in result_records(data.get("answers"), operation):
                    item = object_value(
                        raw, location="native relationship query answer"
                    )
                    index = result_counter(
                        item.get("index"), operation, len(requests) - 1
                    )
                    if index in seen:
                        raise ResourceError("Duplicate native relationship answer")
                    operation.consume("states", 4)
                    operation.consume("bytes", 512)
                    seen.add(index)
                    complete = result_boolean(item.get("complete"))
                    error = result_text(item.get("error"), operation, optional=True)
                    satisfiable = (
                        None
                        if item.get("satisfiable") is None
                        else result_boolean(item["satisfiable"])
                    )
                    if (
                        type(requests[index].focus) is not NamedEntity
                        and satisfiable is not None
                    ) or (wire[index] is None and complete):
                        raise ResourceError(
                            "Native query answer contradicts its qualified role"
                        )
                    groups: list[RelationshipGroup] = []
                    try:
                        _groups(
                            item.get("groups"),
                            requests[index],
                            originals,
                            operation,
                            groups,
                        )
                        if complete and (
                            error is not None
                            or any(not group.complete for group in groups)
                        ):
                            raise ResourceError(
                                "Native relationship completeness contradicts evidence"
                            )
                        local = []
                        if not complete:
                            issue = Issue(
                                "owl.relationship_query_unverified",
                                "Relationship enumeration is incomplete or unsupported",
                                str(index),
                                actual=error,
                                kind="unverified",
                            )
                            issues.append(issue)
                            local.append(issue)
                        report = Report(
                            "owl.inferred_query",
                            int(complete),
                            tuple(local),
                            requested=1,
                            complete=complete,
                        )
                        answers[index] = InferredAnswer(
                            requests[index], tuple(groups), satisfiable, error, report
                        )
                        children.append(report)
                    except LimitExceeded:
                        answers[index] = InferredAnswer(
                            requests[index],
                            tuple(groups),
                            satisfiable,
                            error,
                            Report("owl.inferred_query", requested=1, complete=False),
                        )
                        raise
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native relationship enumeration exhausted its bound"
                    )
                if len(seen) != len(requests):
                    raise ResourceError("Native relationship inventory is incomplete")
                finished = all(
                    answer is not None and answer.report.complete for answer in answers
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.inferred_relationships")
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "owl.relationships_unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "owl.relationships_input_error",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.inferred_relationships")
    pending = tuple(issue for issue in issues if issue.kind != "finding")
    completed = tuple(
        answer
        if answer is not None
        else InferredAnswer(
            requests[index],
            (),
            None,
            native_error,
            Report("owl.inferred_query", issues=pending, requested=1, complete=False),
        )
        for index, answer in enumerate(answers)
    )
    report = Report(
        "owl.inferred_relationships",
        int(consistent is not None)
        + sum(answer.report.complete for answer in completed),
        tuple(issues),
        requested=1 + requested_count,
        complete=finished,
        children=tuple(children),
        regime="owl2-dl-hermit-named-inferred-relationships",
    )
    return InferredContracts(
        completed,
        consistent,
        raw_consistent,
        in_profile,
        native_error,
        originals,
        loaders,
        reasoner,
        capability,
        consistency_report,
        report,
    )
