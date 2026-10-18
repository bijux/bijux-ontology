"""Typed contracts over opaque RDF JSON literal values, not RDF graph edges."""

from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal as Choice

from rdflib import Literal

from bijux_ontology.reports import Report

JSONKind = Choice["object", "array", "string", "integer", "number", "boolean", "null"]


@dataclass(frozen=True)
class JsonRequirement:
    """One concrete JSON value path with conjoined explicit requirements.

    Empty path selects the whole decoded value. expected_json is strict JSON
    text compared structurally: object key order is irrelevant, array order is
    significant, finite numbers compare exactly, and booleans are not numbers.
    This comparison never changes RDF lexical or datatype identity.
    """

    path: tuple[str | int, ...]
    kind: JSONKind | None = None
    expected_json: str | None = None
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    min_items: int | None = None
    max_items: int | None = None


@dataclass(frozen=True)
class JsonLiteralContract:
    """Explicit finite paths plus an optional maintained schema dialect."""

    requirements: tuple[JsonRequirement, ...] = ()
    schema: object | None = None
    dialect: str | None = None


@dataclass(frozen=True)
class JsonLiteralCheck:
    """Original immutable RDF term beside separately owned parsed JSON values.

    JSON numbers are finite Decimal values, preserving numeric precision without
    changing global Decimal settings. value returns an independent copy, including
    lists and dictionaries. A parsed value can remain available beside a failed
    application contract; only report.verified establishes its required checks.
    """

    original: Literal | None
    report: Report
    decoded: bool = False
    _value: object = field(default=None, repr=False, compare=False)

    @property
    def value(self) -> object:
        """Return independent exact parsed values, never shared mutable evidence."""
        return deepcopy(self._value) if self.decoded else None
