"""Immutable typed keys and atomic identifier operation results."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class TemplateKey:
    """A string preserved exactly, or an integer with canonical decimal spelling."""

    name: str
    kind: Literal["string", "integer"] = "string"

    def __post_init__(self) -> None:
        """Validate the explicit scalar declaration without coercing its name."""
        if not isinstance(self.name, str) or not self.name:
            raise InputError("A template key requires a nonempty name")
        if self.kind not in ("string", "integer"):
            raise InputError("Template keys support string or integer values")


@dataclass(frozen=True)
class IdentifierRecord:
    """The exact identifier and its typed bindings in declaration order."""

    iri: str
    bindings: tuple[tuple[str, str | int], ...]


@dataclass(frozen=True)
class IdentifierRecognition:
    """An exact original IRI and a record only after unambiguous recognition."""

    original_iri: str
    record: IdentifierRecord | None
    report: Report


@dataclass(frozen=True)
class IdentifierBatch:
    """All constructed records, or diagnostics without any partial output."""

    records: tuple[IdentifierRecord, ...] | None
    report: Report
