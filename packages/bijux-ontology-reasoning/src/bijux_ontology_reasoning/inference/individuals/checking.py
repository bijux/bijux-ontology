"""Independently qualified realization families sharing one bounded native session."""

from collections.abc import Sequence
from threading import Event
from typing import TYPE_CHECKING

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.values import object_value

from ...axioms.codec.decoding import ProjectionDecoder
from ...axioms.types import NamedEntity, NativeLiteral
from ...backend.owlapi.protocol import (
    result_boolean,
    result_counter,
    result_records,
    result_text,
)
from ...backend.owlapi.receipts import QualifiedReceipt
from ..sessions.types import PreparedReasoningTheory
from .preparation import capture_queries
from .types import (
    IndividualAnswer,
    IndividualQuery,
    IndividualRealization,
    RealizationGroup,
)

if TYPE_CHECKING:
    from ..sessions.runtime import NativeSession


def _groups(
    raw: object,
    groups: list[RealizationGroup],
    query: IndividualQuery,
    operation: Budget,
) -> None:
    """Keep native partial nodes and exact named roles without inventing equality."""
    decoder = ProjectionDecoder(operation)
    role = "class" if query.family == "types" else "named_individual"
    seen: set[NamedEntity] = set()
    for raw_group in result_records(raw, operation):
        item = object_value(raw_group, location="native realization node")
        operation.consume("states", 2)
        operation.consume("bytes", 256)
        complete = result_boolean(item.get("complete"))
        members = []
        try:
            for value in result_records(item.get("members"), operation):
                operation.consume("states", 3)
                operation.consume("bytes", 192)
                member = decoder.entity(value, 1, (role,))
                if member in seen:
                    raise ResourceError("Native realization repeats a named member")
                seen.add(member)
                members.append(member)
        except LimitExceeded:
            groups.append(RealizationGroup(tuple(members), False))
            raise
        if complete and not members:
            raise ResourceError("A complete native realization node cannot be empty")
        groups.append(RealizationGroup(tuple(members), complete))


def _answer(
    raw: object, query: IndividualQuery, index: int, operation: Budget
) -> tuple[IndividualAnswer, bool]:
    """Adopt one paid answer, preserving partial groups on late host exhaustion."""
    issues = IssueCollector(operation)
    groups: list[RealizationGroup] = []
    values: list[NativeLiteral] = []
    policy = error_text = None
    complete = exhausted = False
    try:
        item = object_value(raw, location="native individual answer")
        if result_counter(item.get("index"), operation, index) != index:
            raise ResourceError("Native individual answer order differs from requests")
        policy = result_text(item.get("node_policy"), operation)
        if policy not in ("BY_NAME", "BY_SAME_AS"):
            raise ResourceError("Unknown native individual node policy")
        complete = result_boolean(item.get("complete"))
        exhausted = result_boolean(item.get("exhausted"))
        error_text = result_text(item.get("error"), operation, optional=True)
        _groups(item.get("groups"), groups, query, operation)
        decoder = ProjectionDecoder(operation)
        seen: set[NativeLiteral] = set()
        for encoded in result_records(item.get("values"), operation):
            operation.consume("states", 3)
            operation.consume("bytes", 192)
            value = decoder.literal(encoded, 1)
            if value in seen:
                raise ResourceError("Native realization repeats a literal")
            seen.add(value)
            values.append(value)
        if (query.family == "data_values" and groups) or (
            query.family != "data_values" and values
        ):
            raise ResourceError("Native realization returned another family's values")
        if (
            query.family == "same"
            and complete
            and (len(groups) != 1 or query.focus not in groups[0].members)
        ):
            raise ResourceError(
                "A complete same-individual group must contain its focus"
            )
        if complete and (
            exhausted
            or error_text is not None
            or any(not group.complete for group in groups)
        ):
            raise ResourceError("Complete native realization lacks required coverage")
        if exhausted:
            raise LimitExceeded("Native individual enumeration exhausted its allowance")
        if not complete:
            raise ResourceError(
                "Selected individual query family is unsupported or incomplete; "
                "data values require the finite positive assertion/subproperty/"
                "equivalent-property fragment"
            )
    except LimitExceeded as error:
        complete = False
        exhausted = True
        issues.exhausted(str(error), location="owl.realization")
    except InputError as error:
        complete = False
        try:
            issues.append(Issue("owl.realization", str(error), kind="unverified"))
        except LimitExceeded as stopped:
            exhausted = True
            issues.exhausted(str(stopped), location="owl.realization")
    report = Report(
        "owl.realization.family",
        int(complete),
        tuple(issues),
        requested=1,
        complete=complete,
        regime="prepared-owl2-dl-hermit",
    )
    return IndividualAnswer(
        query,
        tuple(groups) if query.family == "types" else (),
        tuple(groups) if query.family == "instances" else (),
        tuple(groups) if query.family == "object_values" else (),
        tuple(values),
        groups[0] if query.family == "same" and groups else None,
        tuple(groups) if query.family == "different" else (),
        policy,
        error_text,
        report,
    ), exhausted


def realize(
    session: "NativeSession",
    queries: Sequence[IndividualQuery],
    *,
    max_results: int = 10_000,
    theory: PreparedReasoningTheory | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> IndividualRealization:
    """Retrieve selected named consequences; no absent value is an OWL negation."""
    operation = session._operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    answers: list[IndividualAnswer] = []
    requests: tuple[IndividualQuery, ...] = ()
    bound_theory = None
    receipt: QualifiedReceipt | None = None
    capability = None
    consistency = Report("owl.consistency", requested=1, complete=False)
    finished = False
    try:
        with session._locked(operation):
            bound_theory = session.theory
            if type(max_results) is not int or max_results <= 0:
                raise InputError("max_results requires a positive finite integer")
            requests, wire = capture_queries(queries, operation)
            data = session._execute(
                {
                    "operation": "individuals",
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
                raise ResourceError("Ordinary realization requires a consistent theory")
            records = result_records(data.get("individual_queries"), operation)
            if len(records) > len(requests):
                raise ResourceError("Native realization returned unrequested answers")
            for index, raw in enumerate(records):
                answer, stopped = _answer(raw, requests[index], index, operation)
                answers.append(answer)
                children.append(answer.report)
                if stopped:
                    raise LimitExceeded(
                        "Individual enumeration stopped with a paid prefix"
                    )
            if data["status"] == "exhausted":
                raise LimitExceeded("Native individual query coverage is incomplete")
            if len(answers) != len(requests):
                raise ResourceError("Native realization omitted requested families")
            finished = all(answer.report.complete for answer in answers)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error), location="owl.realization")
    except (InputError, OSError) as error:
        session._failure(error, issues)
    report = Report(
        "owl.realization",
        int(receipt is not None and receipt.raw_consistent is not None)
        + sum(answer.report.complete for answer in answers),
        tuple(issues),
        requested=1 + max(1, len(requests)),
        complete=finished,
        children=tuple(children),
        regime="prepared-owl2-dl-hermit",
    )
    return IndividualRealization(
        tuple(answers),
        receipt.raw_consistent if receipt is not None else None,
        bound_theory.identity if bound_theory is not None else None,
        bound_theory.documents if bound_theory is not None else (),
        bound_theory.rdf_sources if bound_theory is not None else (),
        receipt.loaders if receipt is not None else (),
        receipt.reasoner if receipt is not None else None,
        capability,
        consistency,
        report,
    )
