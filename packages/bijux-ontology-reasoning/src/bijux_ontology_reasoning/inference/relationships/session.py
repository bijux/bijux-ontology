"""Named relationships over one existing qualified native classification."""

from collections.abc import Sequence
from threading import Event
from typing import TYPE_CHECKING

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.values import object_value

from ...axioms.types import NamedEntity
from ...backend.owlapi.protocol import (
    result_boolean,
    result_counter,
    result_records,
    result_text,
)
from ...backend.owlapi.receipts import QualifiedReceipt
from ..sessions.types import PreparedReasoningTheory
from .codec import _groups, _query
from .types import InferredAnswer, InferredContracts, InferredQuery, RelationshipGroup

if TYPE_CHECKING:
    from ..sessions.runtime import NativeSession


def query_inferred_contracts(
    session: "NativeSession",
    queries: Sequence[InferredQuery],
    *,
    theory: PreparedReasoningTheory | None = None,
    max_results: int = 10_000,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> InferredContracts:
    """Reuse native named relationships and exact original assertion owners."""
    operation = session._operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    requests: tuple[InferredQuery, ...] = ()
    answers: list[InferredAnswer | None] = []
    receipt: QualifiedReceipt | None = None
    capability = None
    consistency = Report("owl.consistency", requested=1, complete=False)
    finished = False
    bound_theory: PreparedReasoningTheory | None = None
    try:
        with session._locked(operation):
            bound_theory = session.theory
            if type(max_results) is not int or max_results <= 0:
                raise InputError("max_results requires a positive integer")
            requests = capture_items(queries, InferredQuery, operation)
            if not requests:
                raise InputError("No inferred relationship queries were supplied")
            operation.consume("states", len(requests) * 4)
            operation.consume("bytes", 512 * len(requests))
            answers = [None] * len(requests)
            wire = [_query(query, operation) for query in requests]
            data = session._execute(
                {
                    "operation": "relationships",
                    "queries": wire,
                    "max_results": min(max_results, operation.remaining("states")),
                },
                operation,
                issues,
                children,
                theory,
            )
            capability, receipt, consistency = session._header(
                data, operation, issues, children
            )
            if receipt.raw_consistent is not True:
                raise ResourceError(
                    "Ordinary inferred views require a consistent theory"
                )
            seen = set()
            for raw in result_records(data.get("answers"), operation):
                item = object_value(raw, location="prepared inferred answer")
                index = result_counter(item.get("index"), operation, len(requests) - 1)
                if index in seen:
                    raise ResourceError("Duplicate prepared inferred answer")
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
                    or wire[index] is None
                    and complete
                ):
                    raise ResourceError(
                        "Prepared inferred answer changed its typed role"
                    )
                groups: list[RelationshipGroup] = []
                try:
                    _groups(
                        item.get("groups"),
                        requests[index],
                        bound_theory.documents if bound_theory is not None else (),
                        operation,
                        groups,
                    )
                    if complete and (
                        error is not None or any(not g.complete for g in groups)
                    ):
                        raise ResourceError(
                            "Prepared inferred completeness contradicts evidence"
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
                    "Native prepared relationship enumeration exhausted"
                )
            if len(seen) != len(requests):
                raise ResourceError("Prepared inferred inventory is incomplete")
            finished = all(a is not None and a.report.complete for a in answers)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.prepared_relationships")
    except (InputError, OSError) as error:
        session._failure(error, issues)
    pending = tuple(i for i in issues if i.kind != "finding")
    completed = tuple(
        a
        if a is not None
        else InferredAnswer(
            requests[index],
            (),
            None,
            None,
            Report("owl.inferred_query", issues=pending, requested=1, complete=False),
        )
        for index, a in enumerate(answers)
    )
    report = Report(
        "owl.prepared_relationships",
        int(receipt is not None and receipt.raw_consistent is not None)
        + sum(a.report.complete for a in completed),
        tuple(issues),
        requested=1 + len(requests),
        complete=finished,
        children=tuple(children),
        regime="prepared-owl2-dl-hermit",
    )
    return InferredContracts(
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
