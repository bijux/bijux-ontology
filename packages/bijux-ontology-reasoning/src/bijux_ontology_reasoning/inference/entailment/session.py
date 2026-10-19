"""Finite entailment questions over one existing qualified native session."""

from collections.abc import Sequence
from threading import Event
from typing import TYPE_CHECKING, cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits

from ...axioms.types import ProjectedAxiom
from ...backend.owlapi.protocol import result_counter, result_records, result_text
from ...backend.owlapi.receipts import QualifiedReceipt
from ..sessions.types import PreparedReasoningTheory
from .encoding import UnsupportedQuery, capture_obligations, encode_axiom
from .types import EntailmentAnswer, EntailmentCheck

if TYPE_CHECKING:
    from ..sessions.runtime import NativeSession


def check_entailments(
    session: "NativeSession",
    obligations: Sequence[ProjectedAxiom],
    *,
    theory: PreparedReasoningTheory | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> EntailmentCheck:
    """Recompute finite obligations over the current unchanged classification."""
    operation = session._operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    requests: tuple[ProjectedAxiom, ...] = ()
    answers: list[EntailmentAnswer | None] = []
    capability = None
    receipt: QualifiedReceipt | None = None
    consistency = Report("owl.consistency", requested=1, complete=False)
    finished = False
    bound_theory: PreparedReasoningTheory | None = None
    try:
        with session._locked(operation):
            bound_theory = session.theory
            requests = capture_obligations(obligations, operation)
            operation.consume("work", len(requests) * 2)
            answers = [None] * len(requests)
            wire: list[dict[str, object] | None] = []
            unsupported = {}
            for index, axiom in enumerate(requests):
                try:
                    wire.append(encode_axiom(axiom, operation))
                except UnsupportedQuery as error:
                    unsupported[index] = str(error)
                    wire.append(None)
            data = session._execute(
                {"operation": "entailment", "obligations": wire},
                operation,
                issues,
                children,
                theory,
            )
            capability, receipt, consistency = session._header(
                data, operation, issues, children
            )
            if receipt.raw_consistent is not True:
                raise ResourceError("Ordinary entailment requires a consistent theory")
            seen = set()
            for raw in result_records(data.get("answers"), operation):
                item = object_value(raw, location="prepared entailment answer")
                index = result_counter(item.get("index"), operation, len(requests) - 1)
                if index in seen:
                    raise ResourceError("Duplicate prepared entailment answer")
                operation.consume("states", 3)
                operation.consume("bytes", 320)
                seen.add(index)
                value = item.get("entailed")
                query_error = result_text(item.get("error"), operation, optional=True)
                if (
                    type(value) not in (bool, type(None))
                    or ((value is None) != (query_error is not None))
                    or index in unsupported
                    and value is not None
                ):
                    raise ResourceError("Native entailment contradicts its query scope")
                issue = None
                if value is None:
                    issue = Issue(
                        "owl.entailment_query_unverified",
                        unsupported.get(index, "Native query is unsupported"),
                        str(index),
                        kind="unverified",
                        actual=query_error,
                    )
                elif value is False:
                    issue = Issue(
                        "owl.not_entailed",
                        "The required axiom is not entailed",
                        str(index),
                    )
                if issue is not None:
                    issues.append(issue)
                report = Report(
                    "owl.entailment",
                    int(value is not None),
                    () if issue is None else (issue,),
                    requested=1,
                    complete=value is not None,
                )
                answers[index] = EntailmentAnswer(
                    requests[index],
                    "unknown"
                    if value is None
                    else "entailed"
                    if value
                    else "not_entailed",
                    cast(bool | None, value),
                    query_error,
                    report,
                )
                children.append(report)
            if data["status"] == "exhausted":
                raise LimitExceeded("Native prepared entailment exhausted its bounds")
            if len(seen) != len(requests):
                raise ResourceError(
                    "Native prepared entailment omitted required queries"
                )
            finished = all(a is not None and a.report.complete for a in answers)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.prepared_entailment")
    except (InputError, OSError) as error:
        session._failure(error, issues)
    pending = tuple(i for i in issues if i.kind != "finding")
    completed = tuple(
        a
        if a is not None
        else EntailmentAnswer(
            requests[index],
            "unknown",
            None,
            None,
            Report("owl.entailment", issues=pending, requested=1, complete=False),
        )
        for index, a in enumerate(answers)
    )
    report = Report(
        "owl.prepared_entailment",
        int(receipt is not None and receipt.raw_consistent is not None)
        + sum(a.report.complete for a in completed),
        tuple(issues),
        requested=1 + len(requests),
        complete=finished,
        children=tuple(children),
        regime="prepared-owl2-dl-hermit",
    )
    return EntailmentCheck(
        completed,
        receipt.raw_consistent if receipt is not None else None,
        receipt.raw_consistent if receipt is not None else None,
        receipt.in_profile if receipt is not None else None,
        receipt.native_error if receipt is not None else None,
        bound_theory.documents if bound_theory is not None else (),
        receipt.loaders if receipt is not None else (),
        receipt.reasoner if receipt is not None else None,
        capability,
        consistency,
        report,
    )
