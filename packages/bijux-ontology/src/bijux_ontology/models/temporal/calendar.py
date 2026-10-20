"""Maintained XSD calendar arithmetic under a deliberately finite value profile."""

from __future__ import annotations

from decimal import MAX_EMAX, MIN_EMIN, Context, Inexact, Rounded, localcontext
from typing import TYPE_CHECKING

from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.literals.xsd import _datatypes
from bijux_ontology.rdf.value_comparison.observations import ScalarComparisons

if TYPE_CHECKING:
    from elementpath.datatypes import Date, DateTime, DayTimeDuration, YearMonthDuration


class TemporalUnavailable(ResourceError):
    """A valid value lacks the precise ordering/arithmetic this policy needs."""


def _calendar_value_owned(value: Literal, budget: Budget) -> Date | DateTime:
    """Require explicit timezone, supported precision and years 1 through 9999."""
    if value.datatype in (XSD.dateTime, XSD.dateTimeStamp):
        result = ScalarComparisons(budget).calendar_value(value)
        if result is None:
            raise TemporalUnavailable(
                "Calendar timezone, precision or year is unsupported"
            )
        return result
    if value.datatype != XSD.date:
        raise TemporalUnavailable(
            "Partial dates and unanchored times do not establish an order"
        )
    _datatypes()
    from elementpath.datatypes import Date

    budget.consume("resources")
    budget.consume("states", 8)
    text = str(value)
    if len(text) > 4096:
        raise TemporalUnavailable(
            "Calendar lexical form exceeds the supported scalar profile"
        )
    match = Date.pattern.fullmatch(text)
    if match is None:
        raise TemporalUnavailable(
            "Calendar lexical form is outside the supported profile"
        )
    parts = match.groupdict()
    if parts.get("tzinfo") is None or not 1 <= int(parts["year"]) <= 9999:
        raise TemporalUnavailable("Calendar timezone or year is unsupported")
    try:
        return Date.fromstring(text)
    except (ValueError, ArithmeticError) as error:
        raise TemporalUnavailable(
            "Calendar value is outside the supported range"
        ) from error


def _add_duration_owned(
    start: Date | DateTime, duration: Literal, budget: Budget
) -> Date | DateTime:
    """Delegate month/day arithmetic to maintained XSD subtype implementations."""
    classes = _datatypes()
    from elementpath.datatypes import Date, DateTime, DayTimeDuration, YearMonthDuration

    kind = str(duration.datatype).removeprefix(str(XSD))
    if kind not in ("yearMonthDuration", "dayTimeDuration"):
        raise TemporalUnavailable("Choose an explicit supported duration subtype")
    budget.consume("resources")
    budget.consume("states", 8)
    text = str(duration)
    if len(text) > 4096 or classes[kind].pattern.fullmatch(text) is None:
        raise TemporalUnavailable("Duration is outside the supported scalar profile")
    try:
        delta: YearMonthDuration | DayTimeDuration
        delta = (
            YearMonthDuration.fromstring(text)
            if kind == "yearMonthDuration"
            else DayTimeDuration.fromstring(text)
        )
        if isinstance(delta, DayTimeDuration):
            exponent = delta.seconds.as_tuple().exponent
            if not isinstance(exponent, int) or exponent < -6:
                raise TemporalUnavailable(
                    "Duration precision exceeds supported calendar precision"
                )
        if delta.months < 0 or delta.seconds < 0:
            raise TemporalUnavailable(
                "Negative duration bounds are outside this profile"
            )
        if (
            isinstance(start, Date)
            and isinstance(delta, DayTimeDuration)
            and delta.seconds % 86400
        ):
            raise TemporalUnavailable("Date-only duration bounds require whole days")
        result = start + delta
        if not isinstance(result, Date | DateTime):
            raise TemporalUnavailable(
                "Calendar arithmetic returned an incompatible value"
            )
        if not 1 <= result.year <= 9999:
            raise TemporalUnavailable("Calendar arithmetic exceeds supported years")
        return result
    except (ValueError, TypeError, ArithmeticError) as error:
        raise TemporalUnavailable(
            "Calendar duration arithmetic is unavailable"
        ) from error


def _context(size: int) -> Context:
    """Isolate native Decimal arithmetic from the caller's ambient precision."""
    context = Context(prec=max(64, 2 * size + 32), Emax=MAX_EMAX, Emin=MIN_EMIN)
    context.traps[Inexact] = True
    context.traps[Rounded] = True
    return context


def calendar_value(value: Literal, budget: Budget) -> Date | DateTime:
    """Read an explicitly timezoned native value without changing ambient state."""
    with localcontext(_context(len(value))):
        try:
            return _calendar_value_owned(value, budget)
        except (LimitExceeded, ResourceError):
            raise
        except (ValueError, ArithmeticError) as error:
            raise TemporalUnavailable(
                "Exact calendar parsing is unavailable"
            ) from error


def add_duration(
    start: Date | DateTime, duration: Literal, budget: Budget
) -> Date | DateTime:
    """Use exact maintained calendar arithmetic in an isolated Decimal context."""
    with localcontext(_context(len(duration))):
        return _add_duration_owned(start, duration, budget)
