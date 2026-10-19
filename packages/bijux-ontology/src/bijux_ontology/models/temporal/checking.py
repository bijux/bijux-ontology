"""Check explicit chronology on owned text fields without hidden semantic clocks."""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.json.paths import at_path, bound_path, location
from bijux_ontology.json.values import owned_json
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import bound_node, literal_term

from .calendar import add_duration, calendar_value
from .types import (
    RelativeTemporal,
    TemporalCheck,
    TemporalField,
    TemporalInterval,
    TemporalObservation,
    TemporalOrder,
)

if TYPE_CHECKING:
    from elementpath.datatypes import Date, DateTime


@dataclass(frozen=True)
class _Endpoint:
    value: Date | DateTime | None
    datatype: str
    open: bool = False


class _Check:
    """Own one captured record and its shared diagnostics throughout a check."""

    def __init__(self, record: object, operation: Operation) -> None:
        """Keep every selected obligation on the same record and allowance."""
        self.record = record
        self.operation = operation
        self.issues = IssueCollector(operation)
        self.children: list[Report] = []
        self.observations: list[TemporalObservation] = []

    def unknown(self, message: str, where: str) -> None:
        """Retain unavailable ordering separately from a known reversed order."""
        self.issues.append(
            Issue("temporal.order_unverified", message, where, kind="unverified")
        )

    def literal(self, value: Literal, where: str) -> Date | DateTime | None:
        """Keep actual lexical validation required before maintained value parsing."""
        bound_node(value, self.operation)
        report = check_literal(value, location=where)
        retain_report(report, self.operation, self.issues)
        self.children.append(report)
        if not report.verified:
            return None
        try:
            return calendar_value(value, self.operation)
        except ResourceError as error:
            self.unknown(str(error), where)
            return None

    def endpoint(self, field: TemporalField, *, allow_open: bool = False) -> _Endpoint:
        """Select concrete fields; null/absence requires explicit open-end policy."""
        if not isinstance(field, TemporalField):
            raise InputError("Temporal endpoints require typed concrete fields")
        bound_path(field.path, self.operation)
        bound_node(field.datatype, self.operation)
        where = location(field.path)
        self.operation.consume("states", 2)
        try:
            actual = at_path(self.record, field.path)
        except KeyError:
            actual = None
        if actual is None and allow_open:
            self.observations.append(TemporalObservation(field, None, "open"))
            return _Endpoint(None, str(field.datatype), True)
        if type(actual) is not str:
            self.issues.append(
                Issue(
                    "temporal.value_missing",
                    "A required temporal field has no single textual value",
                    where,
                )
            )
            self.observations.append(TemporalObservation(field, None, "invalid"))
            return _Endpoint(None, str(field.datatype))
        from bijux_ontology.execution.limits import bound_text

        bound_text(actual, self.operation)
        literal = Literal(actual, datatype=field.datatype, normalize=False)
        parsed = self.literal(literal, where)
        self.observations.append(
            TemporalObservation(
                field, literal, "ordered" if parsed is not None else "unverified"
            )
        )
        return _Endpoint(parsed, str(field.datatype))

    def ordered(
        self, left: _Endpoint, right: _Endpoint, *, strict: bool, where: str
    ) -> None:
        """Compare only compatible complete calendar families with explicit zones."""
        if left.value is None or right.value is None:
            return
        from rdflib.namespace import XSD

        if (left.datatype == str(XSD.date)) != (right.datatype == str(XSD.date)):
            self.unknown(
                "Date and dateTime precision cannot establish this order", where
            )
            return
        self.operation.consume("work")
        try:
            # Native calendar operators compare local years before applying zones.
            # Their instant representation preserves ordering across year boundaries.
            first, second = left.value.todelta(), right.value.todelta()
            agrees = first < second if strict else first <= second
        except (ValueError, TypeError, ArithmeticError):
            self.unknown("Maintained calendar comparison is indeterminate", where)
            return
        if not agrees:
            self.issues.append(
                Issue(
                    "temporal.order_violation",
                    "Selected temporal order is reversed",
                    where,
                )
            )

    def shifted(self, start: _Endpoint, duration: Literal, where: str) -> _Endpoint:
        """Validate the actual duration before calendar-relative arithmetic."""
        if not isinstance(duration, Literal):
            raise InputError("Temporal offsets require explicit RDF duration literals")
        bound_node(duration, self.operation)
        report = check_literal(duration, location=where)
        retain_report(report, self.operation, self.issues)
        self.children.append(report)
        if not report.verified or start.value is None:
            return _Endpoint(None, start.datatype)
        try:
            return _Endpoint(
                add_duration(start.value, duration, self.operation), start.datatype
            )
        except ResourceError as error:
            self.unknown(str(error), where)
            return _Endpoint(None, start.datatype)


def check_temporal(
    record: object,
    *,
    intervals: Sequence[TemporalInterval] = (),
    chronology: Sequence[TemporalOrder] = (),
    relative: Sequence[RelativeTemporal] = (),
    clock: Literal | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> TemporalCheck:
    """Check timezoned dates/dateTimes, open ends and selected related records.

    Partial Gregorian values, unanchored times, absent zones and excess precision
    remain unknown under this strict policy. Calendar duration bounds use actual
    yearMonthDuration/dayTimeDuration arithmetic; mixed duration is unsupported.
    Only explicit relative rules read the supplied clock. No current time is
    inferred. All original fields remain in the independent returned document.
    Controlled preparation/arithmetic shares the supplied budget; maintained
    scalar internals are in-process, without a hard-preemption claim.
    """
    operation = Operation(limits, budget, timeout, cancel)
    state = _Check(None, operation)
    payload = None
    owned_clock = None
    checked = 0
    requested = 1
    complete = False
    try:
        groups = (
            capture(intervals, operation),
            capture(chronology, operation),
            capture(relative, operation),
        )
        requested = sum(map(len, groups))
        if not requested:
            raise InputError("Select at least one explicit temporal obligation")
        if clock is not None and not groups[2]:
            raise InputError("A semantic clock belongs only to explicit relative rules")
        state.record, cost = owned_json(record, operation)
        if type(state.record) is not dict:
            raise InputError("Temporal checks require an actual JSON record object")
        capacity = 12 * cost + 2
        if capacity > operation.limits.max_bytes:
            raise LimitExceeded("Temporal record exceeds its conservative escape bound")
        operation.consume("bytes", 2 * capacity)
        operation.consume("work", capacity)
        payload = json.dumps(
            state.record, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
        for interval in groups[0]:
            if (
                not isinstance(interval, TemporalInterval)
                or type(interval.open_start) is not bool
                or type(interval.open_end) is not bool
            ):
                raise InputError(
                    "Intervals require typed endpoints and boolean open-end policies"
                )
            start = state.endpoint(interval.start, allow_open=interval.open_start)
            end = state.endpoint(interval.end, allow_open=interval.open_end)
            if start.open and end.open:
                state.unknown(
                    "Both open endpoints establish no finite interval", "interval"
                )
            state.ordered(start, end, strict=False, where="interval")
            for duration, minimum in (
                (interval.minimum_duration, True),
                (interval.maximum_duration, False),
            ):
                if duration is not None:
                    if start.open or end.open:
                        state.unknown(
                            "Duration bounds require both finite endpoints", "interval"
                        )
                    boundary = state.shifted(start, duration, "duration")
                    state.ordered(
                        boundary if minimum else end,
                        end if minimum else boundary,
                        strict=False,
                        where="duration",
                    )
            checked += 1
        for relation in groups[1]:
            if (
                not isinstance(relation, TemporalOrder)
                or type(relation.strict) is not bool
            ):
                raise InputError(
                    "Chronology requires typed fields and an explicit comparison policy"
                )
            state.ordered(
                state.endpoint(relation.before),
                state.endpoint(relation.after),
                strict=relation.strict,
                where="chronology",
            )
            checked += 1
        clock_value = _Endpoint(None, "")
        if groups[2]:
            if clock is None:
                state.unknown(
                    "Relative temporal rules require an explicit clock", "clock"
                )
            elif not isinstance(clock, Literal):
                raise InputError(
                    "A semantic clock requires an explicit RDF calendar literal"
                )
            else:
                bound_node(clock, operation)
                owned_clock = literal_term(
                    str(clock),
                    datatype=str(clock.datatype) if clock.datatype else None,
                    language=clock.language,
                )
                clock_value = _Endpoint(
                    state.literal(owned_clock, "clock"), str(clock.datatype)
                )
        for rule in groups[2]:
            if not isinstance(rule, RelativeTemporal) or rule.relation not in (
                "before",
                "after",
            ):
                raise InputError(
                    "Relative rules require an explicit before/after relation"
                )
            boundary = (
                clock_value
                if rule.offset is None
                else state.shifted(clock_value, rule.offset, "clock-offset")
            )
            actual = state.endpoint(rule.field)
            state.ordered(
                actual if rule.relation == "before" else boundary,
                boundary if rule.relation == "before" else actual,
                strict=True,
                where="clock-relative",
            )
            checked += 1
        operation.consume("work", 0)
        complete = True
    except (LimitExceeded, MemoryError, RecursionError):
        state.issues.exhausted("Temporal execution exceeded its operation allowance")
    except InputError:
        try:
            state.issues.append(
                Issue(
                    "temporal.invalid",
                    "Temporal checks require finite explicit record contracts",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            state.issues.exhausted("Temporal diagnostic allowance exhausted")
    return TemporalCheck(
        Report(
            "temporal.record",
            checked,
            tuple(state.issues),
            requested=max(1, requested),
            complete=complete,
            children=tuple(state.children),
            regime="strict-explicit-calendar-order",
        ),
        tuple(state.observations),
        owned_clock,
        payload,
        operation.limits,
    )
