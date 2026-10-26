"""Qualified finite literal comparisons without rewriting RDF terms.

SPARQL numeric promotion and OWL2 datatype-map identity are distinct operations.
The latter retains datatype categories, signed-zero identity and timezone offset;
it is not XSD facet equality or a general OWL consistency operation.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class _Observation:
    """One complete builtin operand beside its exact original lexical evidence."""

    text: str
    family: str | None
    validation: Report
    datatype: str | None = None
    language: str | None = None
    decimal: Decimal | None = None
    calendar: DateTime | None = None
    native: str | None = None


class ScalarComparisons:
    """One operation's complete builtin operands and maintained expression backend.

    Exact input metadata is captured and bounded at every use. Only successful
    supported builtin observations are retained; pair answers and custom callback
    verdicts are never cached. The context belongs to exactly its original Budget.
    """

    def __init__(self, budget: Budget) -> None:
        """Bind the concrete context without spending another caller allowance."""
        self.budget = budget
        self._observations: (
            dict[
                tuple[str, str | None, str | None, ComparisonRegime, str], _Observation
            ]
            | None
        ) = None
        self._store: ox.Store | None = None
        self._calendar_types: tuple[type[DateTime], type[DateTime]] | None = None
        self._calendar_release: str | None = None
        self._native_release: str | None = None

    def _initialize(self) -> None:
        """Fund the finite context before constructing its owned tables."""
        if self._observations is None:
            self.budget.consume("states", 4)
            self.budget.reserve_storage(1024)
            _text(ox.__version__, self.budget)
            self._native_release = ox.__version__
            self._observations = {}

    def _calendar_backend(self) -> tuple[type[DateTime], type[DateTime]]:
        """Admit actual maintained calendar capability once per operation."""
        if self._calendar_types is not None:
            return self._calendar_types
        self.budget.consume("resources")
        self.budget.consume("states", 8)
        self.budget.reserve_storage(512)
        try:
            release = version("elementpath")
            _text(release, self.budget)
            numbers = tuple(int(part) for part in release.split(".")[:3])
            if len(numbers) != 3 or numbers < (5, 1, 4) or numbers[0] != 5:
                raise ResourceError("Calendar comparison requires bijux-ontology[xsd]")
            from elementpath.datatypes import DateTime, DateTimeStamp
        except (PackageNotFoundError, ImportError, ValueError) as error:
            raise ResourceError(
                "Calendar comparison requires bijux-ontology[xsd] "
                "with elementpath>=5.1.4,<6"
            ) from error
        self._calendar_types = DateTime, DateTimeStamp
        self._calendar_release = release
        return self._calendar_types

    def _calendar_value(self, text: str, stamp: bool) -> DateTime | None:
        """Parse one admitted exact bounded calendar without changing its spelling."""
        classes = self._calendar_backend()
        datatype = classes[int(stamp)]
        self.budget.consume("work", len(text))
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

    def _observe(
        self,
        value: Literal,
        label: str,
        regime: ComparisonRegime,
        registry: DatatypeRegistry | None,
        prior: _Observation | None,
    ) -> _Observation:
        """Capture exact lookup metadata before any retained or parsed allocation."""
        budget = self.budget
        budget.consume("states")
        for text in (value, value.datatype or "", value.language or ""):
            _text(text, budget)
        validate_node(value)
        family = _family(value)
        # Each lookup funds its own spelling snapshot/hash work even on a hit.
        size = len(value) + len(value.datatype or "") + len(value.language or "")
        budget.consume("work", size + 1)
        budget.consume("states", 2)
        budget.reserve_storage(256 + 4 * size)
        lexical = str.__str__(value)
        datatype = str.__str__(value.datatype) if value.datatype is not None else None
        language = str.__str__(value.language) if value.language is not None else None
        capability = (
            self._calendar_release if family in _CALENDAR else self._native_release
        )
        assert self._observations is not None
        if capability is not None:
            key = lexical, datatype, language, regime, capability
            prior = self._observations.get(key)
            if prior is not None:
                return prior
        if (
            prior is not None
            and family is not None
            and prior.validation.verified
            and not prior.validation.issues
            and (prior.text, prior.datatype, prior.language)
            == (lexical, datatype, language)
        ):
            return prior
        if family is None and registry is not None:
            validation = registry.check(value, location=label, budget=budget)
        else:
            validation = check_literal(value, location=label, budget=budget)
        if (
            not validation.verified
            or validation.issues
            or family is None
            or lexical == "NaN"
        ):
            return _Observation(lexical, family, validation, datatype, language)
        if regime == "sparql-1.1" and family == "dateTimeStamp":
            return _Observation(lexical, family, validation, datatype, language)
        # Decimal coefficients, calendar objects and native literal spelling are
        # retained only after their complete lexical proof and funded workspace.
        budget.consume("states", 4)
        budget.reserve_storage(1024 + 16 * size)
        decimal = Decimal(lexical) if family == "decimal" else None
        calendar = None
        native = None
        if family in _CALENDAR:
            # Both original lexical reports precede optional interpretation.
            return _Observation(lexical, family, validation, datatype, language)
        if family in ("float", "double"):
            assert datatype is not None
            native = str(ox.Literal(lexical, datatype=ox.NamedNode(datatype)))
        observation = _Observation(
            lexical, family, validation, datatype, language, decimal, calendar, native
        )
        assert capability is not None
        budget.consume("work", size + 1)
        self._observations[lexical, datatype, language, regime, capability] = (
            observation
        )
        return observation

    def _qualified_calendar(
        self, observed: _Observation, regime: ComparisonRegime
    ) -> DateTime | None:
        """Retain a complete calendar only after the pair's lexical admission."""
        if observed.calendar is not None:
            return observed.calendar
        self._calendar_backend()
        assert self._observations is not None and self._calendar_release is not None
        size = (
            len(observed.text)
            + len(observed.datatype or "")
            + len(observed.language or "")
        )
        self.budget.consume("work", size + 1)
        self.budget.consume("states")
        self.budget.reserve_storage(256 + 4 * size)
        key = (
            observed.text,
            observed.datatype,
            observed.language,
            regime,
            self._calendar_release,
        )
        prior = self._observations.get(key)
        if prior is not None:
            return prior.calendar
        self.budget.consume("states", 2)
        self.budget.reserve_storage(1024 + 16 * size)
        value = self._calendar_value(observed.text, observed.family == "dateTimeStamp")
        if value is None:
            return None
        self.budget.consume("work", size + 1)
        self._observations[key] = _Observation(
            observed.text,
            observed.family,
            observed.validation,
            observed.datatype,
            observed.language,
            calendar=value,
        )
        return value

    def calendar_value(self, value: Literal) -> DateTime | None:
        """Read one qualified finite OWL calendar using this operation's owner."""
        if not isinstance(value, Literal):
            raise InputError("Calendar interpretation requires an RDF literal")
        self._initialize()
        observed = self._observe(value, "value", "owl2-datatype-map", None, None)
        if not observed.validation.verified or observed.family not in _CALENDAR:
            return None
        return self._qualified_calendar(observed, "owl2-datatype-map")

    def _promoted_equal(self, left: str, right: str) -> bool | None:
        """Execute each actual fixed numeric expression on one empty native Store."""
        budget = self.budget
        budget.consume("states", 4)
        # Integer/decimal operands may require native promotion alongside a float.
        budget.reserve_storage(512 + 4 * (len(left) + len(right)))
        query = f"SELECT (({left}) = ({right}) AS ?equal) WHERE {{}}"
        _text(query, budget)
        if self._store is None:
            budget.consume("resources")
            budget.reserve_storage(1024)
            self._store = ox.Store()
        try:
            result = self._store.query(query)
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

    def _native_operand(self, value: Literal, observed: _Observation) -> str:
        """Produce bounded native numeric syntax from the actual qualified operand."""
        if observed.native is not None:
            return observed.native
        size = len(observed.text) + len(value.datatype or "")
        self.budget.consume("states", 2)
        self.budget.reserve_storage(256 + 16 * size)
        return str(
            ox.Literal(observed.text, datatype=ox.NamedNode(str(value.datatype)))
        )

    def _compare(
        self,
        left: Literal,
        right: Literal,
        a: _Observation,
        b: _Observation,
        regime: ComparisonRegime,
    ) -> tuple[bool | None, str]:
        """Evaluate each pair independently under its declared relation."""
        first, second = a.family, b.family
        if first is None or second is None:
            return (
                None,
                "Unsupported datatype value comparison; conversion is not equality",
            )
        if a.text == "NaN" or b.text == "NaN":
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
            x, y = (
                self._qualified_calendar(a, regime),
                self._qualified_calendar(b, regime),
            )
            if x is None or y is None:
                return (
                    None,
                    "Calendar timezone, precision or year is outside qualified scope",
                )
            try:
                same = x.todelta() == y.todelta()
            except (ArithmeticError, OverflowError, ValueError):
                return None, "Calendar instant conversion is outside qualified scope"
            if regime == "owl2-datatype-map":
                same = same and x.tzinfo == y.tzinfo
            return same, ""
        if first == second == "boolean":
            return (a.text in ("true", "1")) == (b.text in ("true", "1")), ""
        if "boolean" in (first, second):
            return (
                (False, "")
                if regime == "owl2-datatype-map"
                else (None, "Incompatible SPARQL operand families")
            )
        if regime == "owl2-datatype-map" and first != second:
            return False, ""
        if first == second == "decimal":
            return a.decimal == b.decimal, ""
        equal = self._promoted_equal(
            self._native_operand(left, a), self._native_operand(right, b)
        )
        if equal and regime == "owl2-datatype-map":
            self.budget.reserve_storage(
                512 + 16 * (len(left.datatype or "") + len(right.datatype or ""))
            )
            left_zero = self._promoted_equal(
                self._native_operand(left, a),
                str(ox.Literal("0", datatype=ox.NamedNode(str(left.datatype)))),
            )
            right_zero = self._promoted_equal(
                self._native_operand(right, b),
                str(ox.Literal("0", datatype=ox.NamedNode(str(right.datatype)))),
            )
            if left_zero is None or right_zero is None:
                return None, "Maintained signed-zero qualification is unavailable"
            if left_zero and right_zero:
                equal = a.text.startswith("-") == b.text.startswith("-")
        return equal, "Maintained numeric conversion did not yield a comparison"

    def compare(
        self,
        left: Literal,
        right: Literal,
        *,
        regime: ComparisonRegime,
        datatype_registry: DatatypeRegistry | None = None,
    ) -> LiteralValueComparison:
        """Compare original operands with fresh pair work and bounded operand reuse."""
        if not isinstance(left, Literal) or not isinstance(right, Literal):
            raise InputError("Value comparison requires two RDF literals")
        if regime not in ("sparql-1.1", "owl2-datatype-map"):
            raise InputError("Select an explicit supported literal comparison regime")
        if datatype_registry is not None and not isinstance(
            datatype_registry, DatatypeRegistry
        ):
            raise InputError("Expected a DatatypeRegistry")
        operation = self.budget
        issues = IssueCollector(operation)
        children: list[Report] = []
        relation: ValueRelation = "indeterminate"
        try:
            operation.consume("work")
            self._initialize()
            operands: list[_Observation] = []
            for label, value in (("left", left), ("right", right)):
                observed = self._observe(
                    value,
                    label,
                    regime,
                    datatype_registry,
                    operands[0] if operands else None,
                )
                children.append(observed.validation)
                operands.append(observed)
            if all(child.verified for child in children):
                if any(value.family is None for value in operands):
                    equal, reason = (
                        None,
                        "Custom conversion supplies no value comparison",
                    )
                else:
                    equal, reason = self._compare(
                        left, right, operands[0], operands[1], regime
                    )
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
