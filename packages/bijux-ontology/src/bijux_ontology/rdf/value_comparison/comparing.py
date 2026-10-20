"""Qualified finite literal comparisons without rewriting RDF terms.

SPARQL numeric promotion and OWL2 datatype-map identity are distinct operations.
The latter retains datatype categories, signed-zero identity and timezone offset;
it is not XSD facet equality or a general OWL consistency operation.
"""

from __future__ import annotations

from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

import pyoxigraph as ox
from rdflib import Literal
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import validate_node

from .types import ComparisonRegime, LiteralValueComparison, ValueRelation

if TYPE_CHECKING:
    from elementpath.datatypes import DateTime

_INTEGER = frozenset(
    (
        "integer",
        "long",
        "int",
        "short",
        "byte",
        "nonNegativeInteger",
        "positiveInteger",
        "nonPositiveInteger",
        "negativeInteger",
        "unsignedLong",
        "unsignedInt",
        "unsignedShort",
        "unsignedByte",
    )
)
_NUMERIC = frozenset(("decimal", "float", "double"))
_CALENDAR = frozenset(("dateTime", "dateTimeStamp"))


def _text(text: str, budget: Budget) -> None:
    """Bound original scalars before validation, encoding or maintained parsing."""
    if len(text) > budget.limits.max_bytes:
        raise LimitExceeded("Literal comparison text exceeds max_bytes")
    budget.consume("work", len(text) + 1)
    try:
        count = len(text.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Literal comparison requires valid Unicode") from error
    if count > budget.limits.max_bytes:
        raise LimitExceeded("Literal comparison text exceeds max_bytes")
    budget.consume("bytes", count)


def _family(value: Literal) -> str | None:
    """Classify only the explicit supported builtin value spaces."""
    datatype = str(value.datatype or (RDF.langString if value.language else XSD.string))
    if not datatype.startswith(str(XSD)):
        return None
    local = datatype[len(str(XSD)) :]
    if local in _INTEGER or local == "decimal":
        return "decimal"
    if local in _NUMERIC or local in _CALENDAR or local == "boolean":
        return local
    return None


def _promoted_equal(left: Literal, right: Literal, budget: Budget) -> bool | None:
    """Use maintained numeric promotion in one fixed, finite local expression.

    Only already validated builtin numeric literals enter the query. There are
    no graph patterns, functions, callbacks, retrieval or caller query text.
    Backend numeric-range errors/unbound answers remain unavailable comparison.
    """
    budget.consume("resources")
    budget.consume("states", 4)
    operands = [
        str(ox.Literal(str(value), datatype=ox.NamedNode(str(value.datatype))))
        for value in (left, right)
    ]
    query = f"SELECT (({operands[0]}) = ({operands[1]}) AS ?equal) WHERE {{}}"
    _text(query, budget)
    try:
        result = ox.Store().query(query)
        if not isinstance(result, ox.QuerySolutions):
            return None
        answers = iter(result)
        row = next(answers)
        budget.consume("work")
        value = row[0]
        if next(answers, None) is not None:
            return None
    except LimitExceeded:
        raise
    except (ValueError, RuntimeError, OverflowError, SyntaxError, StopIteration):
        return None
    if (
        not isinstance(value, ox.Literal)
        or value.datatype.value != str(XSD.boolean)
        or value.value not in ("true", "false")
    ):
        return None
    return value.value == "true"


def _calendar_value(value: Literal, budget: Budget) -> DateTime | None:
    """Use maintained XSD parsing with explicit precision and year qualification."""
    budget.consume("resources")
    budget.consume("states", 8)
    try:
        release = tuple(int(part) for part in version("elementpath").split(".")[:3])
        if len(release) != 3 or release < (5, 1, 4) or release[0] != 5:
            raise ResourceError("Calendar comparison requires bijux-ontology[xsd]")
        from elementpath.datatypes import DateTime, DateTimeStamp
    except (PackageNotFoundError, ImportError, ValueError) as error:
        raise ResourceError(
            "Calendar comparison requires bijux-ontology[xsd] "
            "with elementpath>=5.1.4,<6"
        ) from error
    text = str(value)
    datatype = DateTimeStamp if value.datatype == XSD.dateTimeStamp else DateTime
    budget.consume("work", len(text))
    matched = datatype.pattern.fullmatch(text)
    if matched is None:
        return None
    fields = matched.groupdict()
    fraction = fields.get("microsecond")
    if fraction is not None and len(fraction) > 6:
        return None
    year = int(fields["year"])
    if not 1 <= year <= 9999 or fields.get("tzinfo") is None:
        return None
    try:
        return datatype.fromstring(text)
    except (ValueError, ArithmeticError, OverflowError):
        return None


def _compare(
    left: Literal, right: Literal, regime: ComparisonRegime, budget: Budget
) -> tuple[bool | None, str]:
    """Evaluate only qualified declared families under the selected relation."""
    first, second = _family(left), _family(right)
    if first is None or second is None:
        return None, "Unsupported datatype value comparison; conversion is not equality"
    if str(left) == "NaN" or str(right) == "NaN":
        return None, "NaN has no supported determinate distinct-value relation"
    if regime == "sparql-1.1" and "dateTimeStamp" in (first, second):
        return None, "SPARQL1.1 dateTimeStamp comparison is outside qualified scope"
    if first in _CALENDAR or second in _CALENDAR:
        if first not in _CALENDAR or second not in _CALENDAR:
            return (
                (False, "")
                if regime == "owl2-datatype-map"
                else (None, "Incompatible SPARQL operand families")
            )
        a, b = _calendar_value(left, budget), _calendar_value(right, budget)
        if a is None or b is None:
            return (
                None,
                "Calendar timezone, precision or year is outside qualified scope",
            )
        try:
            same = a.todelta() == b.todelta()
        except (ArithmeticError, OverflowError, ValueError):
            return None, "Calendar instant conversion is outside qualified scope"
        if regime == "owl2-datatype-map":
            same = same and a.tzinfo == b.tzinfo
        return same, ""
    if first == second == "boolean":
        return (str(left) in ("true", "1")) == (str(right) in ("true", "1")), ""
    if "boolean" in (first, second):
        return (
            (False, "")
            if regime == "owl2-datatype-map"
            else (None, "Incompatible SPARQL operand families")
        )
    if regime == "owl2-datatype-map" and first != second:
        return False, ""
    if first == second == "decimal":
        return Decimal(str(left)) == Decimal(str(right)), ""
    equal = _promoted_equal(left, right, budget)
    if equal and regime == "owl2-datatype-map":
        # XSD equality collapses signed zeros; datatype-map identity retains sign.
        left_zero = _promoted_equal(left, Literal("0", datatype=left.datatype), budget)
        right_zero = _promoted_equal(
            right, Literal("0", datatype=right.datatype), budget
        )
        if left_zero is None or right_zero is None:
            return None, "Maintained signed-zero qualification is unavailable"
        if left_zero and right_zero:
            equal = str(left).startswith("-") == str(right).startswith("-")
    return equal, "Maintained numeric conversion did not yield a comparison"


def compare_literal_values(
    left: Literal,
    right: Literal,
    *,
    regime: ComparisonRegime,
    datatype_registry: DatatypeRegistry | None = None,
    budget: Budget | None = None,
) -> LiteralValueComparison:
    """Compare supported literal values while retaining both original RDF terms.

    SPARQL promotes numeric operands. OWL2 datatype-map identity preserves disjoint
    decimal/float/double families, signed zeros and time-zone offsets. Supported
    calendars are explicitly timezoned dateTime (plus OWL dateTimeStamp), years
    1..9999 and
    at most six fractional digits, using the optional maintained XSD backend.
    NaN and custom comparison remain unknown; caller conversion is not equality.
    Bounds cover input/comparison work and the fixed numeric expression, with no
    preemption claim for ordinary maintained in-process scalar operations.
    """
    if not isinstance(left, Literal) or not isinstance(right, Literal):
        raise InputError("Value comparison requires two RDF literals")
    if regime not in ("sparql-1.1", "owl2-datatype-map"):
        raise InputError("Select an explicit supported literal comparison regime")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected a DatatypeRegistry")
    operation = budget or Budget()
    issues = IssueCollector(operation)
    children: list[Report] = []
    families: list[str | None] = []
    relation: ValueRelation = "indeterminate"
    try:
        for label, value in (("left", left), ("right", right)):
            operation.consume("states")
            for text in (value, value.datatype or "", value.language or ""):
                _text(text, operation)
            validate_node(value)
            family = _family(value)
            families.append(family)
            if family is None and datatype_registry is not None:
                validation = datatype_registry.check(
                    value, location=label, budget=operation
                )
            else:
                validation = check_literal(value, location=label, budget=operation)
            children.append(validation)
        if all(child.verified for child in children):
            if None in families:
                equal, reason = None, "Custom conversion supplies no value comparison"
            else:
                equal, reason = _compare(left, right, regime, operation)
            if equal is not None:
                relation = "equal" if equal else "different"
            else:
                issues.append(
                    Issue("literal.value_unverified", reason, kind="unverified")
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except ResourceError as error:
        try:
            issues.append(
                Issue("literal.value_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    return LiteralValueComparison(
        left,
        right,
        regime,
        relation,
        Report(
            "literal.value_comparison",
            int(len(children) == 2),
            tuple(issues),
            requested=1,
            complete=relation != "indeterminate",
            children=tuple(children),
            regime=regime,
        ),
    )
