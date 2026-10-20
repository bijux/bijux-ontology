"""Several named quantity obligations over one captured actual record."""

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.paths import SourcePath

from .checking import (
    PreparedQuantities,
    _contract,
    _diagnostic,
    _lexical,
    _replace,
    _selected,
    _snapshot,
)
from .conversion import _Unavailable
from .types import QuantityCheck, QuantityChecks, QuantityContract


def _contracts(
    contracts: Mapping[str, QuantityContract], budget: Budget
) -> tuple[tuple[str, QuantityContract], ...]:
    """Bound and own a finite named mapping without consuming an arbitrary stream."""
    if not isinstance(contracts, Mapping) or not contracts:
        raise InputError("Choose a nonempty finite mapping of named quantity contracts")
    count = len(contracts)
    budget.consume("states", count)
    budget.reserve_storage(128 + 32 * count)
    names = iter(contracts)
    captured = []
    for _ in range(count):
        budget.consume("work")
        try:
            name = next(names)
            selected = contracts[name]
        except (StopIteration, KeyError) as error:
            raise InputError(
                "Named quantity contracts changed during capture"
            ) from error
        if type(name) is not str or not name:
            raise InputError("Quantity contract names require nonempty strings")
        bound_text(name, budget)
        captured.append((name, selected))
    if next(names, None) is not None:
        raise InputError("Named quantity contracts changed during capture")
    return tuple(captured)


def _field(
    prepared: PreparedQuantities,
    owned: object,
    source_json: str,
    name: str,
    contract: QuantityContract,
    budget: Budget,
) -> QuantityCheck:
    """Keep each selected field's completed findings independent of other fields."""
    issues = IssueCollector(budget)
    original: str | int | None = None
    unit: str | None = None
    reference: Decimal | None = None
    reference_unit: str | None = None
    checked = 0
    try:
        constraints = _contract(contract, budget)
        reference_unit = contract.reference_unit
        if prepared._registry is None or not prepared.report.verified:
            raise _Unavailable(
                "The selected unit inventory was not completely prepared"
            )
        original, unit, reference = _selected(
            prepared, owned, contract, constraints, budget, issues
        )
        checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error), location=name)
    except _Unavailable as error:
        _diagnostic(
            issues, Issue("quantity.unverified", str(error), name, kind="unverified")
        )
    except InputError as error:
        _diagnostic(
            issues, Issue("input.invalid", str(error), name, kind="input_error")
        )
    return QuantityCheck(
        source_json,
        original,
        unit,
        reference,
        reference_unit,
        None,
        Report(
            "quantity",
            checked,
            tuple(issues),
            requested=1,
            complete=checked == 1,
            regime="application",
        ),
    )


def _candidate(
    prepared: PreparedQuantities,
    owned: object,
    contracts: tuple[tuple[str, QuantityContract], ...],
    checks: Mapping[str, QuantityCheck],
    budget: Budget,
    issues: IssueCollector,
) -> str:
    """Coalesce compatible writes, then recheck every exact reference quantity."""
    writes: dict[SourcePath, str] = {}
    for name, selected in contracts:
        value = checks[name].reference_value
        assert value is not None
        for path, replacement in (
            (selected.value_path, _lexical(value, budget)),
            (selected.unit_path, selected.reference_unit),
        ):
            for prior, existing in writes.items():
                budget.consume("work", len(path) + len(prior))
                if path == prior:
                    if existing != replacement:
                        raise _Unavailable(
                            "Selected quantity conversions require conflicting writes"
                        )
                elif path[: len(prior)] == prior or prior[: len(path)] == path:
                    raise _Unavailable(
                        "Selected quantity conversions have overlapping field paths"
                    )
            budget.consume("states")
            budget.reserve_storage(128 + 4 * len(replacement))
            writes[path] = replacement
    for path, replacement in writes.items():
        _replace(owned, path, replacement)
    candidate, payload = _snapshot(owned, budget)
    for name, selected in contracts:
        constraints = _contract(selected, budget)
        _, unit, actual = _selected(
            prepared, candidate, selected, constraints, budget, issues
        )
        if actual != checks[name].reference_value or unit != selected.reference_unit:
            raise _Unavailable(
                "Combined conversion changed an established reference quantity"
            )
    if issues:
        raise _Unavailable(
            "Combined quantity conversion did not pass its required rechecks"
        )
    return payload


def check_quantities(
    prepared: PreparedQuantities,
    record: object,
    contracts: Mapping[str, QuantityContract],
    *,
    convert: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> QuantityChecks:
    """Check several actual fields once; conversion publication requires all of them."""
    current = operation(limits, budget)
    issues = IssueCollector(current)
    source_json = None
    converted_json = None
    checked: dict[str, QuantityCheck] = {}
    requested = 1
    captured: tuple[tuple[str, QuantityContract], ...] = ()
    try:
        if not isinstance(prepared, PreparedQuantities) or type(convert) is not bool:
            raise InputError(
                "Expected prepared quantities and an explicit convert flag"
            )
        issues.retain_completed(prepared.report)
        captured = _contracts(contracts, current)
        requested = len(captured)
        owned, source_json = _snapshot(record, current)
        for name, selected in captured:
            current.consume("states")
            current.reserve_storage(128)
            result = _field(prepared, owned, source_json, name, selected, current)
            checked[name] = result
            issues.retain_completed(result.report)
            if result.report.outcome == "exhausted":
                break
        if (
            convert
            and len(checked) == requested
            and all(result.report.verified for result in checked.values())
            and not issues
        ):
            converted_json = _candidate(
                prepared, owned, captured, checked, current, issues
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
        "quantities",
        len(checked),
        tuple(issues),
        requested=requested,
        complete=len(checked) == requested and bool(captured),
        children=tuple(result.report for result in checked.values()),
        regime="application",
    )
    return QuantityChecks(
        source_json,
        MappingProxyType(checked),
        converted_json if report.verified else None,
        report,
    )
