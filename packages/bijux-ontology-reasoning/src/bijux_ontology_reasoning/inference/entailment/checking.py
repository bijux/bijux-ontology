"""Typed entailment from one owned qualified closure and mature DL lifetime."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Report

from ...axioms.types import ProjectedAxiom
from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import invoke_worker, qualify_capability
from ...backend.owlapi.protocol import result_counter, result_records, result_text
from ...backend.owlapi.receipts import qualify_reasoning_result, reasoning_header
from ...backend.types import LoaderQualification, OwlProfileBackend, ProfileDocument
from ...documents.sources.native import stage_native_documents
from ...documents.sources.rdf import stage_documents
from ...documents.types import OwlDocument
from .encoding import UnsupportedQuery, capture_obligations, encode_axiom
from .types import EntailmentAnswer, EntailmentCheck


def check_entailments(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    obligations: Sequence[ProjectedAxiom],
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> EntailmentCheck:
    """Ask supported typed axiom obligations without conflating two kinds of negative.

    Subclass and named-individual class assertions support existing named,
    complement, Boolean and object existential/universal expressions. Other
    projected types remain required unknown. A negative class assertion is an
    explicit ClassComplement expression. Actual isEntailed false answers mean
    not_entailed; they never establish a negated obligation. Required source
    closure, OWL 2 DL profile and actual consistency precede all query proofs.
    Native work has the same owned heap/deadline as diagnosis; host result
    adoption and retained findings share Budget. No explanation is generated.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    originals: tuple[OwlDocument | ProfileDocument, ...] = ()
    loaders: tuple[LoaderQualification, ...] = ()
    requests: tuple[ProjectedAxiom, ...] = ()
    answers: list[EntailmentAnswer | None] = []
    consistent = raw_consistent = in_profile = None
    native_error = None
    reasoner = capability = None
    consistency_report = Report("owl.consistency", requested=1, complete=False)
    finished = False
    try:
        requests = capture_obligations(obligations, operation)
        # Future unknown-result assembly is prepaid before native execution or
        # caller import-map callbacks, retaining the full requested inventory.
        operation.consume("work", 2 * len(requests))
        answers = [None] * len(requests)
        wire: list[dict[str, object] | None] = []
        unsupported: dict[int, str] = {}
        for index, axiom in enumerate(requests):
            try:
                wire.append(encode_axiom(axiom, operation))
            except UnsupportedQuery as error:
                unsupported[index] = str(error)
                wire.append(None)
        qualify_backend(backend, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="entailment-", dir=backend.artifacts) as job:
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
                raise InputError("Entailment requires an owned RDF or native document")
            maximum = min(operation.remaining("issues"), operation.limits.max_states)
            if not maximum or not operation.remaining("states"):
                raise LimitExceeded("Entailment has no remaining result allowance")
            request.update(
                operation="entailment",
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
            data = invoke_worker(backend, request, directory, operation, heap_mb)
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
                scope="entailment",
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
            operation.consume("states", 2)
            operation.consume("bytes", 256)
            if raw_consistent is not None:
                consistent = raw_consistent
                finding = (
                    Issue("owl.inconsistent", "The selected ontology is inconsistent")
                    if not consistent
                    else None
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
                        "owl.entailment_consistency_unverified",
                        "No ordinary entailment proof is accepted without "
                        "a consistent ontology",
                        kind="unverified",
                    )
                )
                if data["status"] == "exhausted":
                    raise LimitExceeded(
                        "Native consistency precondition did not complete"
                    )
            else:
                seen = set()
                for raw in result_records(data.get("answers"), operation):
                    item = object_value(raw, location="native entailment answer")
                    index = result_counter(
                        item.get("index"), operation, len(requests) - 1
                    )
                    if index in seen:
                        raise ResourceError("Entailment answer ownership is ambiguous")
                    seen.add(index)
                    raw_answer = item.get("entailed")
                    if type(raw_answer) not in (bool, type(None)):
                        raise ResourceError("Invalid native entailment boolean")
                    query_error = result_text(
                        item.get("error"), operation, optional=True
                    )
                    if (raw_answer is None) != (query_error is not None):
                        raise ResourceError(
                            "Entailment answer contradicts its unknown boundary"
                        )
                    if index in unsupported and raw_answer is not None:
                        raise ResourceError("Unsupported host query obtained an answer")
                    operation.consume("states", 2)
                    operation.consume("bytes", 256)
                    query_issue = None
                    if raw_answer is None:
                        query_issue = Issue(
                            "owl.entailment_query_unverified",
                            unsupported.get(
                                index, "Native query scope or operation is unsupported"
                            ),
                            str(index),
                            kind="unverified",
                            actual=query_error,
                        )
                    elif raw_answer is False:
                        query_issue = Issue(
                            "owl.not_entailed",
                            "The required axiom is not entailed",
                            str(index),
                        )
                    if query_issue is not None:
                        issues.append(query_issue)
                    report = Report(
                        "owl.entailment",
                        int(raw_answer is not None),
                        () if query_issue is None else (query_issue,),
                        requested=1,
                        complete=raw_answer is not None,
                    )
                    answers[index] = EntailmentAnswer(
                        requests[index],
                        "unknown"
                        if raw_answer is None
                        else "entailed"
                        if raw_answer
                        else "not_entailed",
                        cast(bool | None, raw_answer),
                        query_error,
                        report,
                    )
                    children.append(report)
                if data["status"] == "exhausted":
                    raise LimitExceeded("Native entailment inventory did not complete")
                if len(seen) != len(requests):
                    raise ResourceError(
                        "Native entailment omitted required obligations"
                    )
                finished = all(
                    answer is not None and answer.outcome != "unknown"
                    for answer in answers
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.entailment")
    except (InputError, OSError):
        try:
            issues.append(
                Issue(
                    "owl.entailment_unverified",
                    "Entailment or its selected interpretation is unavailable",
                    kind="unverified",
                )
            )
        except LimitExceeded as error:
            issues.exhausted(str(error), location="owl.entailment")
    pending_issues = tuple(issue for issue in issues if issue.kind != "finding")
    # Every placeholder below uses the prepaid finite inventory and existing
    # paid stop evidence; no fresh allowance can lose an adopted answer.
    completed = tuple(
        answer
        if answer is not None
        else EntailmentAnswer(
            requests[index],
            "unknown",
            None,
            native_error,
            Report("owl.entailment", 0, pending_issues, requested=1, complete=False),
        )
        for index, answer in enumerate(answers)
    )
    report = Report(
        "owl.entailment_obligations",
        int(consistent is not None)
        + sum(answer.outcome != "unknown" for answer in completed),
        tuple(issues),
        requested=1 + len(requests),
        complete=finished,
        children=tuple(children),
        regime="owl2-dl-hermit-1.4.5.519",
    )
    return EntailmentCheck(
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
