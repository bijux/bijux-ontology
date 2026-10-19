"""Prepare one bounded, explicit native unit inventory for repeated checks."""

from collections.abc import Sequence
from decimal import Context, Decimal, localcontext
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from bijux_ontology._jsonschema import capture, operation
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.reports import Issue, Report

from .checking import PreparedQuantities
from .types import UnitBinding

_SUPPORTED = frozenset(
    (
        "meter",
        "millimeter",
        "centimeter",
        "kilometer",
        "second",
        "minute",
        "hour",
        "kelvin",
        "degree_Celsius",
    )
)


def _diagnostic(issues: IssueCollector, issue: Issue) -> None:
    """Retain a bounded preparation failure or the terminal exhaustion marker."""
    try:
        issues.append(issue)
    except LimitExceeded as error:
        issues.exhausted(str(error))


def prepare_quantities(
    bindings: Sequence[UnitBinding],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparedQuantities:
    """Prepare the declared exact atomic profile using the optional native engine.

    Decimal initialization is isolated from ambient precision and traps. Only
    the documented exact atomic definitions can be selected afterwards, so
    unrelated approximate constants in Pint's default definitions are unused.
    """
    current = operation(limits, budget)
    issues = IssueCollector(current)
    owned: list[UnitBinding] = []
    units: dict[str, Any] = {}
    registry: Any = None
    checked = 0
    requested = 1
    try:
        captured = capture(bindings, current)
        requested = max(1, len(captured))
        seen: set[str] = set()
        for binding in captured:
            if not isinstance(binding, UnitBinding):
                raise InputError("Expected explicit UnitBinding records")
            bound_text(binding.code, current)
            bound_text(binding.pint_unit, current)
            if not binding.code or not binding.pint_unit:
                raise InputError("Unit codes and native names must be nonempty")
            code, name = str(binding.code), str(binding.pint_unit)
            if code in seen:
                raise InputError("Each unit code requires one unambiguous binding")
            current.consume("states", 2)
            seen.add(code)
            owned.append(UnitBinding(code, name))
            if binding.pint_unit not in _SUPPORTED:
                issues.append(
                    Issue(
                        "quantity.unit_unverified",
                        "Native unit is outside the exact atomic conversion profile",
                        kind="unverified",
                        actual=binding.pint_unit,
                    )
                )
        if not owned:
            raise InputError("Quantity preparation requires explicit unit bindings")
        try:
            import pint

            release = version("Pint")
        except (ImportError, PackageNotFoundError) as error:
            raise ResourceError(
                "Quantity checking requires bijux-ontology[quantities]"
            ) from error
        bound_text(release, current)
        parts = release.split(".")
        if len(parts) != 3 or parts[:2] != ["0", "25"] or not parts[2].isdigit():
            raise ResourceError("Quantity checking requires Pint>=0.25.3,<0.26")
        if int(parts[2]) < 3:
            raise ResourceError("Quantity checking requires Pint>=0.25.3,<0.26")
        current.consume("resources")
        current.consume("work", 1024)
        with localcontext(Context(prec=128, Emin=-999999, Emax=999999)):
            registry = pint.UnitRegistry(
                non_int_type=Decimal,
                cache_folder=None,
                default_as_delta=False,
                autoconvert_offset_to_baseunit=False,
            )
            for binding in owned:
                current.consume("work")
                if binding.pint_unit in _SUPPORTED:
                    units[binding.code] = registry.parse_units(
                        binding.pint_unit, as_delta=False
                    )
                    checked += 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except ResourceError as error:
        _diagnostic(
            issues,
            Issue(
                "quantity.backend_unverified",
                str(error),
                kind="unverified",
            ),
        )
    except InputError as error:
        _diagnostic(issues, Issue("input.invalid", str(error), kind="input_error"))
    except Exception:
        _diagnostic(
            issues,
            Issue(
                "quantity.backend_unverified",
                "Native unit preparation is unavailable",
                kind="unverified",
            ),
        )
    report = Report(
        "quantities.preparation",
        checked,
        tuple(issues),
        requested=requested,
        complete=checked == requested,
        regime="application",
    )
    return PreparedQuantities._owned(tuple(owned), registry, units, report)
