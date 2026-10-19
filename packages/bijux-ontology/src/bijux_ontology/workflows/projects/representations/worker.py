"""Owned acquisition and maintained semantic checks for negotiated variants."""

import json
import sys
import time
from dataclasses import asdict, replace
from email.message import Message
from typing import Any, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.projects.acquisition.closure import Closure
from bijux_ontology.workflows.projects.acquisition.http import (
    HttpAcquisition,
    _Response,
)
from bijux_ontology.workflows.projects.acquisition.types import (
    AcquisitionPolicy,
    _Unavailable,
)

from .types import PublishedResource, RepresentationVariant


class _PublicationBudget(ControlBudget):
    """Keep internal storage inside the host grant and text inside wire capacity."""

    def __init__(
        self, limits: Limits, allowances: object, evidence_bytes: object
    ) -> None:
        super().__init__(limits, allowances)
        if type(evidence_bytes) is not int or not 0 <= evidence_bytes <= self.remaining(
            "bytes"
        ):
            raise InputError("Invalid publication evidence allowance")
        self._evidence_bytes = evidence_bytes
        self._evidence_used = 0

    def consume(self, kind: BudgetKind, amount: int = 1) -> None:
        """Bound all exportable semantic text before retaining its evidence."""
        if type(amount) is not int or amount < 0:
            raise InputError("Budget consumption must be a nonnegative integer")
        if kind == "bytes" and amount > self._evidence_bytes - self._evidence_used:
            raise LimitExceeded("Publication exhausted its evidence allowance")
        super().consume(kind, amount)
        if kind == "bytes":
            self._evidence_used += amount

    def reserve_storage(self, amount: int) -> None:
        """Charge unexported scratch and copies to the actual shared byte grant."""
        super().consume("bytes", amount)


def _finding(
    issues: IssueCollector,
    code: str,
    message: str,
    expected: str,
    actual: str,
) -> None:
    """Own bounded original response metadata before retaining a known mismatch."""
    issues.append(Issue(code, message, expected=expected, actual=actual))


def _response(
    response: _Response,
    variant: RepresentationVariant,
    expected_url: str,
    issues: IssueCollector,
) -> None:
    """Qualify response identity and profile separately from the requested Accept."""
    item = response.resource
    if item.effective_url != expected_url:
        _finding(
            issues,
            "publication.identity_mismatch",
            "Actual effective representation URL differs from its explicit binding",
            expected_url,
            item.effective_url,
        )
    if item.media_type != variant.media_type:
        _finding(
            issues,
            "publication.media_mismatch",
            "Actual representation media differs from its required variant",
            variant.media_type,
            item.media_type,
        )
    header = Message()
    header["Content-Type"] = response.content_type
    profiles = [
        value
        for name, value in (header.get_params() or ())[1:]
        if name.lower() == "profile"
    ]
    if len(profiles) > 1:
        raise InputError("Ambiguous representation profile parameters")
    if variant.profile is not None:
        actual = profiles[0] if profiles else ""
        if not isinstance(actual, str):
            raise InputError("Malformed representation profile parameter")
        bound_text(actual, issues.budget)
        if actual != variant.profile:
            _finding(
                issues,
                "publication.profile_mismatch",
                "Actual representation profile differs from its required variant",
                variant.profile,
                actual,
            )


def _prepared(
    response: _Response,
    contract: PublishedResource,
    closure: Closure,
    operation: Operation,
    issues: IssueCollector,
    children: list[Report],
) -> PreparedSource | None:
    """Reuse original cached JSON-LD responses or ordinary native RDF parsing."""
    resource = response.resource
    descriptor = RdfSource(
        response.payload,
        format="json-ld"
        if resource.media_type in ("application/json", "application/ld+json")
        else resource.media_type,
        document_iri=contract.iri,
        effective_url=resource.effective_url,
        graph_name=contract.graph_name,
    )
    closure._register(descriptor)
    if resource.media_type in ("application/json", "application/ld+json"):
        processor()
        prepared = prepare_jsonld(
            resource.effective_url,
            base=resource.effective_url,
            document_iri=contract.iri,
            document_loader=closure.context,
            budget=operation,
        )
        retain_report(prepared.report, operation, issues)
        children.append(prepared.report)
        if prepared.source is None or not prepared.report.verified:
            return None
        source = prepared.source
        if contract.graph_name is not None:
            source._read(contract.graph_name, budget=operation)
            # This is the operation's newly owned private snapshot, not a caller
            # view; retain the already qualified physical selection for compare.
            source.graph_name = contract.graph_name
    else:
        source = prepare_source(
            descriptor,
            budget=operation,
            require_complete_graphs=True,
            document_loader=closure.context,
        )
        children.append(Report("publication.source", 1, regime="supplied-rdf"))
    if source.graph_fidelity != "complete":
        issues.append(
            Issue(
                "publication.graph_scope_unverified",
                "Representation does not establish complete empty-graph inventory",
                kind="unverified",
            )
        )
        return None
    if source.is_dataset and source.graph_name is None:
        default = source._read("@default", budget=operation).identifier
        for graph in source._owned_contexts(operation):
            operation.consume("work")
            load_vocabulary(
                source,
                graph_name="@default"
                if graph.identifier == default
                else graph.identifier,
                imports=closure,
                document_loader=closure.context,
                budget=operation,
                _owned_worker=True,
            )
    else:
        load_vocabulary(
            source,
            imports=closure,
            document_loader=closure.context,
            budget=operation,
            _owned_worker=True,
        )
    children.append(Report("publication.required_resources", 1))
    return source


def _variant(
    contract: PublishedResource,
    variant: RepresentationVariant,
    http: HttpAcquisition,
    reference: PreparedSource | None,
    index: int,
    operation: Operation,
) -> tuple[PreparedSource | None, Report]:
    """Keep every actual raw preparation/closure/comparison child once."""
    issues = IssueCollector(operation)
    children: list[Report] = []
    source = None
    complete = False
    observed = False
    try:
        response = http.get(contract.url)
        observed = True
        _response(response, variant, variant.effective_url or contract.url, issues)
        if not issues:
            source = _prepared(
                response,
                contract,
                Closure(None, {}, {}, http),
                operation,
                issues,
                children,
            )
        if source is not None and not issues:
            if index == 0:
                complete = True
            elif reference is None:
                issues.append(
                    Issue(
                        "publication.reference_unverified",
                        "Required reference representation is unavailable",
                        kind="unverified",
                    )
                )
            else:
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("Publication comparison exceeded its deadline")
                compared = compare_rdf(
                    reference,
                    source,
                    budget=operation,
                    timeout_seconds=remaining,
                    _owned_worker=True,
                )
                retain_report(compared, operation, issues)
                children.append(compared)
                complete = compared.complete
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error), location=variant.name)
    except (InputError, ResourceError) as error:
        try:
            bound_text(str(error), operation)
            issues.append(
                Issue(
                    "publication.variant_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError | _Unavailable)
                    else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location=variant.name)
    if not complete and not any(
        issue.kind in ("unverified", "input_error", "exhausted") for issue in issues
    ):
        try:
            issues.append(
                Issue(
                    "execution.incomplete",
                    "Required RDF comparison did not execute for this response",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location=variant.name)
    report = Report(
        variant.name,
        int(observed),
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="rdf-publication"
        if contract.graph_name is None
        else "rdf-publication-graph-projection",
    )
    return source, report


def _run(request: dict[str, Any], operation: Operation) -> dict[str, object]:
    """Aggregate real request counts across independent fixed-Accept caches."""
    raw = request["resource"]
    variants = tuple(RepresentationVariant(**item) for item in raw["variants"])
    contract = PublishedResource(raw["iri"], raw["url"], variants, raw["graph_name"])
    policy = AcquisitionPolicy(**request["policy"])
    issues = IssueCollector(operation)
    records: list[dict[str, object]] = []
    children: list[Report] = []
    remaining = policy.max_requests
    reference = None
    operation.consume("states", len(variants))
    try:
        for index, variant in enumerate(variants):
            operation.consume("work")
            if remaining <= 0:
                raise LimitExceeded("Publication exceeds aggregate max_requests")
            http = HttpAcquisition(
                replace(policy, max_requests=remaining),
                operation,
                accept=variant.accept,
            )
            operation.consume("states", 2)
            record: dict[str, object] = {
                "index": index,
                "resource": None,
                "content_type": None,
                "resources": [],
                "requests": 0,
            }
            records.append(record)
            try:
                source, report = _variant(
                    contract,
                    variant,
                    http,
                    reference,
                    index,
                    operation,
                )
                children.append(report)
                if index == 0 and report.verified:
                    reference = source
            finally:
                remaining -= http.requests
                record["requests"] = http.requests
            # Record original response/dependency evidence before later scope
            # adoption can stop; cached aliases keep their actual Link metadata.
            for cached in http.responses.values():
                operation.consume("work")
                operation.consume("states")
                cast(list[object], record["resources"]).append(asdict(cached.resource))
            root_response = http.responses.get(contract.url)
            if root_response is not None:
                record["resource"] = asdict(root_response.resource)
                record["content_type"] = root_response.content_type
            retain_report(report, operation, issues)
            if report.outcome == "exhausted":
                break
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error), location="publication")
    report = Report(
        "publication.representations",
        sum(child.checked for child in children),
        tuple(issues),
        requested=len(variants),
        complete=len(children) == len(variants)
        and all(child.complete for child in children),
        children=tuple(children),
        regime="rdf-publication"
        if contract.graph_name is None
        else "rdf-publication-graph-projection",
    )
    return {"report": report.to_dict(), "variants": records}


def process_request(payload: bytes, cap: int) -> bytes:
    """Use existing typed report records and remaining-allowance receipts."""
    request = decode_json(payload, max_bytes=cap)
    if not isinstance(request, dict):
        raise InputError("Malformed representation request")
    budget = _PublicationBudget(
        Limits(**request["limits"]), request["allowances"], request["evidence_bytes"]
    )
    operation = Operation(None, budget, request["timeout_ns"] / 1_000_000_000, None)
    result = _run(request, operation)
    result["consumed"] = budget.consumed()
    # The host charged three protocol character allowances before granting
    # semantic work: traversal, encoding and host decoding. This control budget
    # spends the first two prepaid allowances; it never renews semantic work.
    control = Budget(Limits(max_bytes=cap, max_total_bytes=cap, max_work=2 * cap))
    json_bound(result, control)
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))
    for chunk in encoder.iterencode(result):
        control.consume("work", len(chunk))
        if len(output) + len(chunk) > cap:
            raise LimitExceeded("Publication result exceeds protocol cap")
        control.consume("bytes", len(chunk))
        output.extend(chunk.encode("ascii"))
    return bytes(output)


def main() -> int:
    """Run the trusted installed entry point through bounded protocol pipes."""
    cap = int(sys.argv[1])
    try:
        output = process_request(sys.stdin.buffer.read(cap + 1), cap)
    except (InputError, MemoryError, RecursionError):
        output = b'{"status":"exhausted"}'
    sys.stdout.buffer.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
