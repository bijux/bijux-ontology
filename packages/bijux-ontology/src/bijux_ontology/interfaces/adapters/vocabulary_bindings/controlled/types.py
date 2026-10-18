"""Finite controlled-set expressions and captured record binding decisions."""

from dataclasses import dataclass
from typing import TypeAlias

from rdflib import Literal, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology._jsonpaths import SourcePath
from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Report
from bijux_ontology.skos.subsets.types import ConceptSubset


@dataclass(frozen=True)
class AllowedUnion:
    """Permit a term when at least one supplied operand permits it."""

    operands: tuple["AllowedExpression", ...]


@dataclass(frozen=True)
class AllowedIntersection:
    """Permit a term only when every supplied operand permits it."""

    operands: tuple["AllowedExpression", ...]


@dataclass(frozen=True)
class AllowedDifference:
    """Permit a left member only when the right operand proves its exclusion."""

    left: "AllowedExpression"
    right: "AllowedExpression"


AllowedExpression: TypeAlias = (
    ConceptSubset
    | ValueEnumeration
    | AllowedUnion
    | AllowedIntersection
    | AllowedDifference
)


@dataclass(frozen=True)
class BindingCase:
    """An exact text trait selects one explicitly authored allowed expression."""

    name: str
    equals: str
    allowed: AllowedExpression


@dataclass(frozen=True)
class CompiledBindingCase:
    """One owned discriminator value and its reusable finite membership evidence."""

    name: str
    equals: str
    enumeration: ValueEnumeration


@dataclass(frozen=True, init=False)
class CompiledBindings:
    """Immutable compiled memberships; construction cannot bypass qualification."""

    discriminator: SourcePath
    cases: tuple[CompiledBindingCase, ...]
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InputError("Use compile_bindings to qualify controlled binding cases")

    @classmethod
    def _owned(
        cls,
        discriminator: SourcePath,
        cases: tuple[CompiledBindingCase, ...],
        report: Report,
    ) -> "CompiledBindings":
        """Adopt only bounded compiled fields after the operation completes."""
        result = object.__new__(cls)
        object.__setattr__(result, "discriminator", discriminator)
        object.__setattr__(result, "cases", cases)
        object.__setattr__(result, "report", report)
        return result


@dataclass(frozen=True, init=False)
class BindingSelection:
    """Actual captured record, selected case and required condition evidence.

    Document access returns an independent decoded copy. The selected enumeration
    carries this decision's report so ordinary native output checks also require
    its actual discriminator proof. Unsupported/missing selection has no fallback.
    """

    case: str | None
    trait: str | None
    enumeration: ValueEnumeration | None
    report: Report
    _record_json: bytes | None
    _limits: Limits

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InputError("Use select_binding to capture an actual record")

    @classmethod
    def _owned(
        cls,
        case: str | None,
        trait: str | None,
        enumeration: ValueEnumeration | None,
        report: Report,
        record_json: bytes | None,
        limits: Limits,
    ) -> "BindingSelection":
        """Adopt immutable record bytes and already-qualified decision fields."""
        result = object.__new__(cls)
        for name, value in (
            ("case", case),
            ("trait", trait),
            ("enumeration", enumeration),
            ("report", report),
            ("_record_json", record_json),
            ("_limits", limits),
        ):
            object.__setattr__(result, name, value)
        return result

    @property
    def document(self) -> object:
        """Return a fresh copy of the actual independently captured record."""
        if self._record_json is None:
            return None
        return decode_json(
            self._record_json,
            max_bytes=self._limits.max_bytes,
            max_depth=self._limits.max_depth,
        )

    def closed_values(self) -> tuple[URIRef | Literal, ...]:
        """Expose SHACL-compatible values only after complete closed qualification."""
        if (
            self.enumeration is None
            or not self.enumeration.qualified
            or not self.report.verified
        ):
            raise InputError("A closed value list requires a verified binding decision")
        return self.enumeration.values
