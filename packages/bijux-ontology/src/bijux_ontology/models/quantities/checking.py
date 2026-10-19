"""Exact selected-record quantity checks using one prepared native unit engine."""

import json
from decimal import Context, Decimal, DecimalException, Inexact, Rounded, localcontext
from sys import getsizeof
from threading import RLock
from typing import Any, cast

from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import (
    at_path,
    bound_path,
    bounded_location,
    validate_path,
)
from bijux_ontology.json.values import owned_json
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.rdf.literals import check_literal

from .types import QuantityCheck, QuantityContract, UnitBinding


class PreparedQuantities:
    """Own finite bindings and native caches without exposing mutable registry state.

    A lock serializes native factor-cache access and permanently unavailable
    ordered pairs. This protects repeated exactness qualification; it does not
    turn in-process native calls into hard interruptible worker operations.
    """

    _bindings: tuple[UnitBinding, ...]
    _report: Report
    _registry: Any
    _units: dict[str, Any]
    _failed: set[tuple[str, str]]
    _lock: RLock

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require preparation to establish bindings and the actual native plan."""
        raise InputError("Use prepare_quantities to establish a native unit plan")

    @classmethod
    def _owned(
        cls,
        bindings: tuple[UnitBinding, ...],
        registry: Any,
        units: dict[str, Any],
        report: Report,
    ) -> "PreparedQuantities":
        """Retain only the preparation operation's bounded, actual native plan."""
        result = object.__new__(cls)
        result._bindings = bindings
        result._report = report
        result._registry = registry
        result._units = units
        result._failed = set()
        result._lock = RLock()
        return result

    @property
    def bindings(self) -> tuple[UnitBinding, ...]:
        """Expose the immutable finite caller binding inventory."""
        return self._bindings

    @property
    def report(self) -> Report:
        """Retain the actual preparation qualification without mutable aliases."""
        return self._report

    def check(
        self,
        record: object,
        contract: QuantityContract,
        *,
        convert: bool = False,
        limits: Limits | None = None,
        budget: Budget | None = None,
    ) -> QuantityCheck:
        """Check actual selected fields with the caller's cumulative allowance."""
        return check_quantity(
            self, record, contract, convert=convert, limits=limits, budget=budget
        )


class _Unavailable(Exception):
    """A required quantity interpretation has no supported exact conversion."""


class _DimensionMismatch(Exception):
    """Native dimensional analysis proved incompatible bound dimensions."""


def _diagnostic(issues: IssueCollector, issue: Issue) -> None:
    """Bound ordinary failure metadata while retaining a terminal exhaustion."""
    try:
        issues.append(issue)
    except LimitExceeded as error:
        issues.exhausted(str(error))


def _snapshot(record: object, budget: Budget) -> tuple[object, str]:
    """Reserve escaped JSON storage before encoding an independently owned tree."""
    owned, count = owned_json(record, budget)
    size = 16 * (count + 1)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Quantity record representation exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", size)
    return owned, json.dumps(owned, ensure_ascii=False, separators=(",", ":"))


def _contract(contract: QuantityContract, budget: Budget) -> ValueConstraints:
    """Qualify every public contract field before path lookup or numeric matching."""
    if not isinstance(contract, QuantityContract):
        raise InputError("Expected an explicit QuantityContract")
    for path in (contract.value_path, contract.unit_path):
        validate_path(path)
        bound_path(path, budget)
        if not path:
            raise InputError("Quantity fields require nonempty record paths")
    if (
        contract.value_path[: len(contract.unit_path)] == contract.unit_path
        or contract.unit_path[: len(contract.value_path)] == contract.value_path
    ):
        raise InputError("Quantity value and unit paths must be independent fields")
    bound_text(contract.reference_unit, budget)
    if not contract.reference_unit:
        raise InputError("Quantity reference unit must be explicit")
    for value in (contract.minimum, contract.maximum):
        if value is not None:
            if not isinstance(value, Decimal) or not value.is_finite():
                raise InputError("Quantity bounds require finite Decimal values")
            size = 128 + 8 * getsizeof(value)
            if size > budget.limits.max_bytes:
                raise LimitExceeded("Quantity bound formatting exceeds max_bytes")
            budget.consume("bytes", size)
            budget.consume("work", size)
            bound_text(str(value), budget)
    if (
        type(contract.minimum_inclusive) is not bool
        or type(contract.maximum_inclusive) is not bool
    ):
        raise InputError("Quantity bound inclusivity must be explicit booleans")
    return ValueConstraints(
        minimum=contract.minimum,
        maximum=contract.maximum,
        minimum_inclusive=contract.minimum_inclusive,
        maximum_inclusive=contract.maximum_inclusive,
    )


def _numeric(value: object, budget: Budget, issues: IssueCollector) -> Decimal | None:
    """Interpret only maintained valid exact integer or decimal lexical forms."""
    if type(value) is int:
        if value.bit_length() > 4096:
            raise _Unavailable("Integer exceeds the exact quantity scalar profile")
        text = str(value)
        datatype = XSD.integer
    elif type(value) is str:
        text = value
        datatype = XSD.decimal
    else:
        raise _Unavailable("Quantity numeric fields require integers or decimal text")
    bound_text(text, budget)
    if len(text) > 4096:
        raise _Unavailable("Decimal text exceeds the qualified lexical profile")
    budget.consume("work", len(text) + 1)
    report = check_literal(Literal(text, datatype=datatype, normalize=False))
    retain_report(report, budget, issues)
    return Decimal(text) if report.verified else None


def _convert(
    prepared: PreparedQuantities,
    value: Decimal,
    code: str,
    reference: str,
    budget: Budget,
) -> Decimal:
    """Use native dimension and conversion semantics with persistent pair failure."""
    from pint.errors import DimensionalityError

    bound_text(code, budget)
    bound_text(reference, budget)
    budget.consume("work", len(str(value)) + 1)
    if code not in prepared._units or reference not in prepared._units:
        raise _Unavailable("Quantity unit has no supported explicit binding")
    pair = (str(prepared._units[code]), str(prepared._units[reference]))
    precision = max(64, len(str(value)) + 32)
    budget.consume("states", precision + 1)
    budget.consume("bytes", precision * 16)
    budget.consume("work", precision * precision)
    context = Context(prec=precision, Emin=-999999, Emax=999999)
    context.traps[Inexact] = True
    context.traps[Rounded] = True
    with prepared._lock:
        if pair in prepared._failed:
            raise _Unavailable("An earlier native conversion was not exact")
        try:
            with localcontext(context):
                quantity = prepared._registry.Quantity(value, prepared._units[code])
                converted = quantity.to(prepared._units[reference]).magnitude
        except DimensionalityError as error:
            raise _DimensionMismatch from error
        except DecimalException as error:
            prepared._failed.add(pair)
            raise _Unavailable(
                "Native conversion cannot establish an exact decimal"
            ) from error
    if not isinstance(converted, Decimal) or not converted.is_finite():
        raise _Unavailable("Native conversion did not produce a finite decimal")
    return converted


def _lexical(value: Decimal, budget: Budget) -> str:
    """Reserve fixed-point lexical expansion before formatting the native value."""
    size = getsizeof(value)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Converted quantity exceeds max_bytes")
    budget.consume("states", size)
    budget.consume("bytes", size * 8)
    parts = value.as_tuple()
    digits = len(parts.digits)
    exponent = cast(int, parts.exponent)
    length = max(digits + max(exponent, 0), 2 + max(-exponent, digits)) + 1
    if length > budget.limits.max_bytes:
        raise LimitExceeded("Converted quantity lexical expansion exceeds max_bytes")
    budget.consume("bytes", length)
    budget.consume("work", length)
    return format(value, "f")


def _selected(
    prepared: PreparedQuantities,
    record: object,
    contract: QuantityContract,
    constraints: ValueConstraints,
    budget: Budget,
    issues: IssueCollector,
) -> tuple[str | int | None, str | None, Decimal | None]:
    """Check actual selected scalars without borrowing any declared sample values."""
    try:
        value = at_path(record, contract.value_path)
        unit = at_path(record, contract.unit_path)
    except KeyError:
        issues.append(
            Issue(
                "quantity.field_missing",
                "A required value or unit field is absent",
                details=None,
            )
        )
        return None, None, None
    if not isinstance(unit, str):
        raise _Unavailable("Quantity unit fields require an explicit string code")
    bound_text(unit, budget)
    number = _numeric(value, budget, issues)
    original = cast(str | int, value) if type(value) in (str, int) else None
    if number is None:
        return original, unit, None
    try:
        converted = _convert(prepared, number, unit, contract.reference_unit, budget)
    except LimitExceeded:
        raise
    except _Unavailable:
        raise
    except Exception as error:
        if isinstance(error, _DimensionMismatch):
            issues.append(
                Issue(
                    "quantity.dimension_mismatch",
                    "The bound unit is incompatible with the reference dimension",
                    bounded_location(contract.unit_path, budget),
                )
            )
            return original, unit, None
        raise _Unavailable("The native quantity operation is unsupported") from error
    lexical = _lexical(converted, budget)
    valid = check_literal(Literal(lexical, datatype=XSD.decimal, normalize=False))
    retain_report(valid, budget, issues)
    if not valid.verified:
        return original, unit, None
    matched = application_matches(
        constraints,
        Literal(lexical, datatype=XSD.decimal, normalize=False),
        budget=budget,
        lexical_valid=True,
    )
    if matched is False:
        issues.append(
            Issue(
                "quantity.bound_mismatch",
                "Converted value violates reference-unit bounds",
                bounded_location(contract.value_path, budget),
            )
        )
    elif matched is None:
        raise _Unavailable("Reference-unit bounds could not be evaluated")
    return original, unit, converted


def _replace(record: object, path: tuple[str | int, ...], value: str) -> None:
    """Replace one already established field on the private owned JSON tree."""
    parent = at_path(record, path[:-1])
    part = path[-1]
    if isinstance(parent, dict) and isinstance(part, str):
        cast(dict[str, object], parent)[part] = value
    elif isinstance(parent, list) and type(part) is int:
        cast(list[object], parent)[part] = value
    else:
        raise InputError("The selected quantity field changed during conversion")


def check_quantity(
    prepared: PreparedQuantities,
    record: object,
    contract: QuantityContract,
    *,
    convert: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> QuantityCheck:
    """Check an actual captured quantity and optionally recheck a separate candidate."""
    current = operation(limits, budget)
    issues = IssueCollector(current)
    source_json: str | None = None
    converted_json: str | None = None
    original_value: str | int | None = None
    original_unit: str | None = None
    reference_value: Decimal | None = None
    reference_unit: str | None = None
    checked = 0
    try:
        if not isinstance(prepared, PreparedQuantities) or type(convert) is not bool:
            raise InputError(
                "Expected prepared quantities and an explicit convert flag"
            )
        retain_report(prepared.report, current, issues)
        constraints = _contract(contract, current)
        reference_unit = str(contract.reference_unit)
        owned, source_json = _snapshot(record, current)
        if prepared._registry is None or not prepared.report.verified:
            raise _Unavailable(
                "The selected unit inventory was not completely prepared"
            )
        original_value, original_unit, reference_value = _selected(
            prepared, owned, contract, constraints, current, issues
        )
        checked = 1
        if convert and reference_value is not None and not issues:
            lexical = _lexical(reference_value, current)
            _replace(owned, contract.value_path, lexical)
            _replace(owned, contract.unit_path, reference_unit)
            candidate, payload = _snapshot(owned, current)
            _, actual_unit, actual_value = _selected(
                prepared, candidate, contract, constraints, current, issues
            )
            if (
                actual_value == reference_value
                and actual_unit == reference_unit
                and not issues
            ):
                converted_json = payload
            else:
                raise _Unavailable(
                    "Converted fields did not retain the checked quantity"
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except RecursionError:
        issues.exhausted("Quantity record nesting exceeds supported recursion")
    except _Unavailable as error:
        _diagnostic(issues, Issue("quantity.unverified", str(error), kind="unverified"))
    except InputError as error:
        _diagnostic(issues, Issue("input.invalid", str(error), kind="input_error"))
    report = Report(
        "quantity",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1,
        regime="application",
    )
    return QuantityCheck(
        source_json,
        original_value,
        original_unit,
        reference_value,
        reference_unit,
        converted_json if report.verified else None,
        report,
    )
