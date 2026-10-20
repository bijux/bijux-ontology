"""Shared exact scalar unit conversion without a document or field adapter."""

from __future__ import annotations

from decimal import Context, Decimal, DecimalException, Inexact, Rounded, localcontext
from typing import TYPE_CHECKING

from bijux_ontology.execution.limits import Budget, bound_text

if TYPE_CHECKING:
    from .checking import PreparedQuantities


class _Unavailable(Exception):
    """A required quantity interpretation has no supported exact conversion."""


class _DimensionMismatch(Exception):
    """Native dimensional analysis proved incompatible bound dimensions."""


def convert_scalar(
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
