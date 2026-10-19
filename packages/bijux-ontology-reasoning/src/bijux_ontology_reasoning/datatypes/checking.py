"""Owned mature datatype reductions with explicit original and temporary scope."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Location, Report

from ..axioms.decoding import ProjectionDecoder
from ..axioms.types import DataProperty, DataRange, PropertyDomainRange
from ..backend.bundles import qualify_backend
from ..backend.execution import (
    invoke_worker,
    qualify_capability,
    result_counter,
    result_text,
)
from ..backend.qualification import _records, qualify_reasoning_result, reasoning_header
from ..backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ..documents.sources.native import stage_native_documents
from ..documents.sources.rdf import stage_documents
from ..documents.types import OwlDocument
from .encoding import capture_requests, encode_request
from .types import (
    DatatypeAnswer,
    DatatypeCheck,
    DatatypeFieldBranch,
    DatatypeObligation,
    DatatypeOutcome,
    DatatypeRequest,
)


def _ranges(raw: object, operation: Budget) -> tuple[PropertyDomainRange, ...]:
    """Retain actual range projections and every unsupported-node boundary."""
    item = object_value(raw, location="datatype range projection")
    if _records(item.get("unsupported"), operation):
        raise ResourceError("A required global range has unsupported OWL projection")
    decoder = ProjectionDecoder(operation)
    records = _records(item.get("axioms"), operation)
    operation.consume("bytes", 64 + 128 * len(records))
    result = []
    for record in records:
        axiom = decoder.axiom(record)
        if (
            not isinstance(axiom, PropertyDomainRange)
            or axiom.kind != "range"
            or not isinstance(axiom.property, DataProperty)
        ):
            raise ResourceError("Datatype evidence is not a property-range axiom")
        result.append(axiom)
    return tuple(result)


def _range(raw: object, operation: Budget) -> DataRange:
    """Retain the native combined range through the existing typed projection."""
    values = _ranges(raw, operation)
    if len(values) != 1:
        raise ResourceError("Native datatype reduction omitted its exact range")
    return cast(DataRange, values[0].expression)


def _boolean(raw: object) -> bool | None:
    if type(raw) not in (bool, type(None)):
        raise ResourceError("Native datatype answer is not an exact boolean")
    return cast(bool | None, raw)


def _location(request: DatatypeRequest, index: int) -> str:
    if isinstance(request, DatatypeFieldBranch):
        return request.field.location or request.field.name
    return request.location or str(index)


def _check(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    raw: Sequence[DatatypeObligation]
    | Sequence[FieldDeclaration]
    | Sequence[DatatypeFieldBranch],
    *,
    fields: bool,
    branches: bool = False,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None,
    limits: Limits | None,
    budget: Budget | None,
    timeout: float,
    heap_mb: int,
    cancel: Event | None,
) -> DatatypeCheck:
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    originals: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    requests: tuple[DatatypeRequest, ...] = ()
    answers: list[DatatypeAnswer | None] = []
    consistent = raw_consistent = temporary = fresh_absent = in_profile = None
    native_error = fresh = None
    reasoner = capability = None
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    finished = False
    try:
        requests = capture_requests(raw, operation, fields=fields, branches=branches)
        operation.consume("work", 2 * len(requests))
        answers = [None] * len(requests)
        wire: list[dict[str, object] | None] = []
        unsupported: dict[int, str] = {}
        for index, request in enumerate(requests):
            try:
                wire.append(encode_request(request, operation))
            except ResourceError as encoding_error:
                unsupported[index] = str(encoding_error)
                wire.append(None)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="datatype-", dir=backend.artifacts) as job:
            directory = Path(job)
            if isinstance(source, OwlDocument):
                originals, payload = stage_native_documents(
                    source,
                    cast(Mapping[str, OwlDocument] | None, imports),
                    directory,
                    operation,
                )
                kind = "native"
            elif isinstance(source, PreparedSource):
                originals, payload, _ = stage_documents(
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
                    "Datatype checks require owned RDF or native OWL documents"
                )
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded(
                    "Datatype operation has no remaining result allowance"
                )
            payload.update(
                operation="datatypes",
                input_kind=kind,
                obligations=wire,
                max_bytes=operation.limits.max_bytes,
                max_issues=maximum,
                max_resources=operation.limits.max_resources,
                max_nodes=operation.remaining("states"),
                query_depth=max(1, min(64, operation.limits.max_depth // 4)),
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            data = invoke_worker(backend, payload, directory, operation, heap_mb)
            operation.consume("states", 4)
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
                scope="datatypes",
            )
            loaders, reasoner = receipt.loaders, receipt.reasoner
            if not receipt.qualified:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native datatype input qualification did not complete"
                    )
                raise ResourceError("Selected datatype reasoning scope is unqualified")
            operation.consume("states", 4)
            operation.consume("bytes", 512)
            if raw_consistent is not None:
                consistent = raw_consistent
                finding = (
                    None
                    if consistent
                    else Issue(
                        "owl.inconsistent", "Selected original ontology is inconsistent"
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
                raise ResourceError(
                    "Datatype proofs require a consistent original ontology"
                )
            fresh = result_text(data.get("fresh_property"), operation, optional=True)
            fresh_absent, temporary = (
                _boolean(data.get("fresh_absent")),
                _boolean(data.get("temporary_consistent")),
            )
            if fresh is None or fresh_absent is not True or temporary is not True:
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Fresh datatype temporary theory did not qualify"
                    )
                raise ResourceError("Datatype temporary theory is not qualified")
            seen = set()
            for raw_answer in _records(data.get("answers"), operation):
                item = object_value(raw_answer, location="datatype answer")
                index = result_counter(item.get("index"), operation, len(requests) - 1)
                if index in seen:
                    raise ResourceError("Datatype answer ownership is ambiguous")
                seen.add(index)
                difference = _boolean(item.get("difference_satisfiable"))
                intersection = _boolean(item.get("intersection_satisfiable"))
                query_error = result_text(item.get("error"), operation, optional=True)
                request = requests[index]
                field = isinstance(request, DatatypeFieldBranch)
                known = query_error is None and (
                    difference is not None and intersection is not None
                    if isinstance(request, DatatypeFieldBranch)
                    else difference is not None
                    if request.relation == "inclusion"
                    else intersection is not None
                )
                if query_error is not None and (
                    difference is not None or intersection is not None
                ):
                    raise ResourceError(
                        "Unsupported native datatype answer contains a verdict"
                    )
                if index in unsupported and known:
                    raise ResourceError(
                        "Unsupported host branch received a native verdict"
                    )
                actual = required = None
                witnesses: tuple[PropertyDomainRange, ...] = ()
                if known:
                    try:
                        actual, required = (
                            _range(item.get("actual"), operation),
                            _range(item.get("required"), operation),
                        )
                        witnesses = _ranges(item.get("ranges"), operation)
                        if field and not witnesses:
                            raise ResourceError(
                                "Required field range inventory is empty"
                            )
                    except ResourceError as projection_error:
                        known = False
                        query_error = str(projection_error)
                operation.consume("states", 3)
                operation.consume("bytes", 384)
                outcome: DatatypeOutcome = "unknown"
                issue = None
                if not known:
                    issue = Issue(
                        "owl.datatype_unverified",
                        unsupported.get(
                            index, "Native datatype relationship is unsupported"
                        ),
                        _location(request, index),
                        kind="unverified",
                        actual=query_error,
                    )
                elif isinstance(request, DatatypeFieldBranch):
                    outcome = (
                        "included"
                        if difference is False
                        else "disjoint"
                        if intersection is False
                        else "overlap"
                    )
                    if outcome != "included":
                        issue = Issue(
                            "owl.datatype_conflict"
                            if outcome == "disjoint"
                            else "owl.datatype_not_included",
                            "Declared literal range is disjoint from "
                            "its global property range"
                            if outcome == "disjoint"
                            else "Declared literal range is not included in "
                            "its global property range",
                            _location(request, index),
                        )
                elif request.relation == "inclusion":
                    outcome = "included" if difference is False else "not_included"
                    if outcome == "not_included":
                        issue = Issue(
                            "owl.datatype_not_included",
                            "Required datatype inclusion does not hold",
                            _location(request, index),
                        )
                else:
                    outcome = "disjoint" if intersection is False else "not_disjoint"
                    if outcome == "not_disjoint":
                        issue = Issue(
                            "owl.datatype_not_disjoint",
                            "Required datatype disjointness does not hold",
                            _location(request, index),
                        )
                if issue is not None:
                    issues.append(issue)
                report = Report(
                    "owl.datatype",
                    int(known),
                    () if issue is None else (issue,),
                    requested=1,
                    complete=known,
                )
                answers[index] = DatatypeAnswer(
                    request,
                    outcome,
                    actual,
                    required,
                    witnesses,
                    difference,
                    intersection,
                    query_error,
                    report,
                )
                children.append(report)
            if data["status"] == "exhausted":
                raise LimitExceeded("Native datatype inventory did not complete")
            if len(seen) != len(requests):
                raise ResourceError(
                    "Native datatype inventory omitted required obligations"
                )
            finished = all(
                answer is not None and answer.outcome != "unknown" for answer in answers
            )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.datatypes")
    except (InputError, OSError) as failure:
        try:
            unavailable = isinstance(failure, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.datatypes_unverified"
                    if unavailable
                    else "owl.datatypes_input_error",
                    str(failure) or "Datatype reasoning is unavailable",
                    failure.location if isinstance(failure, InputError) else "",
                    kind="unverified" if unavailable else "input_error",
                    details=Location.from_error(failure),
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="owl.datatypes")

    pending = tuple(issue for issue in issues if issue.kind != "finding")
    completed = tuple(
        answer
        if answer is not None
        else DatatypeAnswer(
            requests[index],
            "unknown",
            None,
            None,
            (),
            None,
            None,
            native_error,
            Report("owl.datatype", 0, pending, requested=1, complete=False),
        )
        for index, answer in enumerate(answers)
    )
    report = Report(
        "owl.datatype_obligations",
        int(consistent is not None) + sum(a.outcome != "unknown" for a in completed),
        tuple(issues),
        requested=1 + len(requests),
        complete=finished,
        children=tuple(children),
        regime="owl2-dl-hermit-1.4.5.519",
    )
    return DatatypeCheck(
        completed,
        consistent,
        raw_consistent,
        temporary,
        fresh,
        fresh_absent,
        in_profile,
        native_error,
        originals,
        loaders,
        reasoner,
        capability,
        consistency_report,
        report,
    )


def check_datatype_obligations(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    obligations: Sequence[DatatypeObligation],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> DatatypeCheck:
    """Check supported universal datatype relationships with a fresh data property.

    A finite successful literal sample cannot establish these obligations.
    Native datatype/facet limits, missing closure and inconsistency stay unknown.
    """
    return _check(
        backend,
        source,
        obligations,
        fields=False,
        imports=imports,
        limits=limits,
        budget=budget,
        timeout=timeout,
        heap_mb=heap_mb,
        cancel=cancel,
    )


def check_datatype_fields(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    fields: Sequence[FieldDeclaration],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> DatatypeCheck:
    """Check every branch against conjoined selected global data-property ranges.

    Actual maintained super/equivalent-property inference locates inherited range
    axioms. Missing ranges and unsupported branches remain required unknown.
    Source samples are retained, never extrapolated; application value constraints
    and class-context/model co-emission obligations are separate operations.
    """
    return _check(
        backend,
        source,
        fields,
        fields=True,
        imports=imports,
        limits=limits,
        budget=budget,
        timeout=timeout,
        heap_mb=heap_mb,
        cancel=cancel,
    )


def check_datatype_branches(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    branches: Sequence[DatatypeFieldBranch],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> DatatypeCheck:
    """Check explicitly selected original branches without changing field identity.

    Consumers combining resource and literal checks retain their own required
    remainder. Automatic check_datatype_fields continues to request every branch.
    """
    return _check(
        backend,
        source,
        branches,
        fields=False,
        branches=True,
        imports=imports,
        limits=limits,
        budget=budget,
        timeout=timeout,
        heap_mb=heap_mb,
        cancel=cancel,
    )
