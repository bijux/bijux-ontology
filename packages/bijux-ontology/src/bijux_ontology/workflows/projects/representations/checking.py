"""Finite negotiated publication checks through the existing owned transport."""

import json
import threading
import time
from dataclasses import asdict, replace

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, report_from_data
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    BUDGET_KINDS,
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.json.values import decode_json
from bijux_ontology.workflows.projects.acquisition.types import (
    AcquiredResource,
    AcquisitionPolicy,
    _http_url,
)

from .types import (
    PublishedResource,
    RepresentationAgreement,
    RepresentationVariant,
    VariantAgreement,
    qualify_variant,
)


def _resource(value: object, operation: Budget) -> AcquiredResource | None:
    """Adopt exact bounded observed response fields, never an inferred variant."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(
        AcquiredResource.__dataclass_fields__
    ):
        raise ResourceError("Malformed publication response metadata")
    for field in (
        "requested_url",
        "effective_url",
        "media_type",
        "encoding",
        "sha256",
    ):
        if type(value[field]) is not str:
            raise ResourceError("Publication response fields require text")
        bound_text(value[field], operation)
    context = value["context_url"]
    if context is not None:
        if type(context) is not str:
            raise ResourceError("Publication context URL requires text")
        bound_text(context, operation)
    for field in ("compressed_bytes", "expanded_bytes"):
        number = value[field]
        if type(number) is not int or not 0 <= number <= operation.limits.max_bytes:
            raise ResourceError("Invalid publication response byte count")
    operation.consume("states")
    return AcquiredResource(**value)


def _execute(
    resource: PublishedResource,
    policy: AcquisitionPolicy,
    operation: Operation,
) -> tuple[dict[str, object], Report]:
    """Reserve bounded host handoff before granting the worker semantic work."""
    unit = min(
        min(operation.remaining("bytes"), operation.remaining("work")) // 32,
        max(0, operation.remaining("states") // 4 - 512),
    )
    if unit < 1024:
        raise LimitExceeded("Insufficient publication protocol allowance")
    cap = min(
        operation.limits.max_bytes,
        operation.remaining("work") // 4,
        operation.remaining("bytes") // 6,
        max(0, operation.remaining("states") - 512) * 8,
    )
    # The fixed typed Report/Issue encoding uses at least sixteen JSON bytes
    # per registered report/issue reference, including empty child scopes.
    references = max(1, cap // 16)
    operation.consume("bytes", 2 * cap + 64 * references)
    operation.consume("work", 3 * cap + references)
    operation.consume("states", 128 + references)
    request = {
        "resource": asdict(resource),
        "policy": asdict(policy),
        "limits": asdict(operation.limits),
        "allowances": {kind: operation.remaining(kind) for kind in BUDGET_KINDS},
        "evidence_bytes": operation.remaining("bytes"),
        "timeout_ns": max(
            1, int((operation.deadline - time.monotonic()) * 1_000_000_000)
        ),
    }
    size = json_bound(request, operation)
    if size > cap:
        raise LimitExceeded("Publication request exceeds protocol capacity")
    operation.consume("bytes", size)
    grants = {kind: operation.remaining(kind) for kind in BUDGET_KINDS}
    # Each selected scope has at most four copies of paid diagnostic text
    # across its fixed aggregate/variant/raw report tree. ASCII JSON needs at
    # most six characters per original UTF-8 byte. Reserve fixed report/record
    # fields separately; authored names and terminal anchors can recur without
    # a semantic text charge, so cover their bounded request representation.
    static = 4096 * (1 + len(resource.variants)) + 8 * size
    if static >= cap:
        raise LimitExceeded("Insufficient publication evidence capacity")
    # Internal preparation storage shares the actual host byte grant, while
    # exportable evidence retains its independently prepaid wire allowance.
    request["evidence_bytes"] = min(grants["bytes"], (cap - static) // (4 * 6))
    request["allowances"] = grants
    payload = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode(
        "ascii"
    )
    remaining = operation.deadline - time.monotonic()
    if remaining <= 0:
        raise LimitExceeded("Publication exceeded its deadline")
    received = False
    try:
        raw = execute_worker(
            "bijux_ontology.workflows.projects.representations.worker",
            payload,
            cap=cap,
            timeout=remaining,
            cancel=operation.cancel,
        )
        # JSON decoding and terminal receipt registration use the pre-reserved host
        # capacity, even if cancellation arrives after the worker completed.
        data = decode_json(raw, max_bytes=cap)
        if isinstance(data, dict) and data.get("status") == "exhausted":
            raise LimitExceeded("Publication worker exhausted its protocol allowance")
        if not isinstance(data, dict) or set(data) != {
            "report",
            "variants",
            "consumed",
        }:
            raise ResourceError("Malformed publication worker result")
        consumed = data["consumed"]
        if (
            not isinstance(consumed, dict)
            or set(consumed) != set(BUDGET_KINDS)
            or any(
                type(consumed[kind]) is not int
                or not 0 <= consumed[kind] <= grants[kind]
                for kind in BUDGET_KINDS
            )
        ):
            raise ResourceError("Malformed publication worker receipt")
        report = report_from_data(data["report"])
        if report.requested != len(resource.variants) or len(report.children) > len(
            resource.variants
        ):
            raise ResourceError(
                "Publication report lost its requested variant inventory"
            )
        for kind in BUDGET_KINDS:
            operation.consume_completed(kind, consumed[kind])
        operation.shared._register_transferred_report(
            report,
            paid_issues=consumed["issues"],
            max_references=references,
        )
        received = True
        return data, report
    finally:
        if not received:
            # An admitted worker without a qualified receipt cannot refund its grant.
            for kind in BUDGET_KINDS:
                operation.consume_completed(
                    kind, min(grants[kind], operation.remaining(kind))
                )


def check_representations(
    resource: PublishedResource,
    *,
    policy: AcquisitionPolicy,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> RepresentationAgreement:
    """Acquire declared variants and compare their actual selected RDF content.

    Explicit effective URL bindings qualify redirects; allowed origins alone do
    not establish representation identity. Every variant and its actual context
    and required-import closure is required. A graph-selected result qualifies
    only that physical projection, never complete publication equivalence.
    Requests, redirects and dependencies share an aggregate policy cap. Network,
    parsing and comparison occur in the owned deadline/cancellation process;
    native internals have no claimed instruction or RSS counter.
    """
    if not isinstance(resource, PublishedResource) or not isinstance(
        policy, AcquisitionPolicy
    ):
        raise InputError(
            "Publication checks require a typed resource and acquisition policy"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    records: list[VariantAgreement] = []
    publication = url = graph_name = None
    requested = len(resource.variants)
    completed = None
    adopted = False
    try:
        bound_text(resource.iri, operation)
        publication = resource.iri
        _http_url(resource.url, operation)
        url = resource.url
        if resource.graph_name is not None:
            bound_text(resource.graph_name, operation)
            graph_name = resource.graph_name
        selected = capture_items(resource.variants, RepresentationVariant, operation)
        names = set()
        for variant in selected:
            qualify_variant(variant, operation)
            if variant.name in names:
                raise InputError("Required representation names must be distinct")
            operation.consume("states", 2)
            names.add(variant.name)
            records.append(VariantAgreement(variant))
        for origin in policy.origins:
            bound_text(origin, operation)
        data, completed = _execute(resource, policy, operation)
        retain_report(completed, operation, issues)
        raw_records = data["variants"]
        if not isinstance(raw_records, list) or len(raw_records) != len(
            completed.children
        ):
            raise ResourceError("Publication result omitted its actual variant scopes")
        total = 0
        for index, raw_record in enumerate(raw_records):
            operation.consume("work")
            if (
                not isinstance(raw_record, dict)
                or set(raw_record)
                != {
                    "index",
                    "resource",
                    "content_type",
                    "resources",
                    "requests",
                }
                or type(raw_record["index"]) is not int
                or raw_record["index"] != index
            ):
                raise ResourceError(
                    "Publication result reordered its required variants"
                )
            attempts = raw_record["requests"]
            if type(attempts) is not int or not 0 <= attempts <= policy.max_requests:
                raise ResourceError("Invalid publication request count")
            total += attempts
            if total > policy.max_requests:
                raise ResourceError("Publication exceeded its aggregate request cap")
            dependencies = raw_record["resources"]
            if not isinstance(dependencies, list):
                raise ResourceError("Publication dependencies require an array")
            operation.consume("states", len(dependencies))
            retained = []
            for value in dependencies:
                observed = _resource(value, operation)
                if observed is None:
                    raise ResourceError(
                        "Publication dependencies require actual metadata"
                    )
                retained.append(observed)
            content_type = raw_record["content_type"]
            if content_type is not None:
                if type(content_type) is not str:
                    raise ResourceError("Publication Content-Type requires text")
                bound_text(content_type, operation)
            records[index] = replace(
                records[index],
                resource=_resource(raw_record["resource"], operation),
                content_type=content_type,
                resources=tuple(retained),
                requests=attempts,
                report=completed.children[index],
            )
        adopted = True
    except LimitExceeded as error:
        if completed is not None:
            issues.retain_completed(completed)
        issues.exhausted(str(error), location="publication")
    except (InputError, ResourceError):
        try:
            issues.append(
                Issue(
                    "publication.input_unverified",
                    "Required publication input or worker evidence is unqualified",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="publication")
    if adopted and completed is not None:
        report = completed
    else:
        report = Report(
            "publication.representations",
            0,
            tuple(issues),
            requested=requested,
            complete=False,
            children=(completed,) if completed is not None else (),
            regime="rdf-publication"
            if resource.graph_name is None
            else "rdf-publication-graph-projection",
        )
    return RepresentationAgreement(
        publication,
        url,
        "complete" if resource.graph_name is None else "graph-projection",
        graph_name,
        tuple(records),
        report,
    )
