"""Explicit unit bindings, selected quantity contracts and owned results."""

import json
from dataclasses import dataclass
from decimal import Decimal

from bijux_ontology._jsonpaths import SourcePath
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class UnitBinding:
    """Assign one exact caller unit code or IRI string to a native unit name."""

    code: str
    pint_unit: str


@dataclass(frozen=True)
class QuantityContract:
    """Select an exact quantity pair and bounds in its explicit reference unit.

    Numeric fields contain JSON integers or XSD decimal lexical strings. This
    application contract does not change RDF identity or establish UCUM syntax.
    """

    value_path: SourcePath
    unit_path: SourcePath
    reference_unit: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True


@dataclass(frozen=True)
class QuantityCheck:
    """Retain source spelling independently of an explicitly requested conversion.

    Each document access returns a fresh ordinary JSON value. A conversion is
    exposed only after its actual selected fields pass intrinsic postconditions;
    callers can apply their ordinary model or consumer checks to that record.
    """

    source_json: str | None
    original_value: str | int | None
    original_unit: str | None
    reference_value: Decimal | None
    reference_unit: str | None
    converted_json: str | None
    report: Report

    @property
    def source(self) -> object:
        """Return an independent source record without a mutable ownership alias."""
        return json.loads(self.source_json) if self.source_json is not None else None

    @property
    def converted(self) -> object:
        """Return only a complete, intrinsically checked conversion candidate."""
        return (
            json.loads(self.converted_json)
            if self.converted_json is not None and self.report.verified
            else None
        )
