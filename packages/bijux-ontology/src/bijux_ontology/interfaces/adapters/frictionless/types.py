"""Explicit semantic mappings and actual native package cell evidence."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from rdflib import Literal as RdfLiteral
from rdflib import URIRef

from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class ColumnBinding:
    """Map a named native field through an explicit RDF predicate and value mode."""

    field: FieldDeclaration
    mode: Literal["literal", "iri"] = "literal"

    def __post_init__(self) -> None:
        """Require explicit forward predicates and supported source policies."""
        if not isinstance(self.field, FieldDeclaration) or self.mode not in (
            "literal",
            "iri",
        ):
            raise InputError("Columns require a FieldDeclaration and literal/iri mode")
        if self.field.predicate in ("@id", "@type") or self.field.reverse:
            raise InputError(
                "Package columns require an ordinary forward RDF predicate"
            )
        if self.field.key is not None:
            raise InputError(
                "Native column names are explicit; declaration aliases are unsupported"
            )


@dataclass(frozen=True)
class ResourceBinding:
    """Select a resource and an absolute-IRI native string subject column.

    Every selected row is checked independently before its assertions enter the
    combined graph. Repeated subjects therefore cannot hide missing cell output.
    """

    resource: str
    subject_column: str
    columns: tuple[ColumnBinding, ...]

    def __post_init__(self) -> None:
        """Keep the finite mapping inventory explicit and unambiguous."""
        if (
            type(self.resource) is not str
            or not self.resource
            or type(self.subject_column) is not str
            or not self.subject_column
        ):
            raise InputError("Bindings require resource and subject column names")
        if (
            type(self.columns) is not tuple
            or not self.columns
            or any(not isinstance(column, ColumnBinding) for column in self.columns)
        ):
            raise InputError(
                "Resource bindings require a nonempty tuple of ColumnBinding"
            )
        if len({column.field.name for column in self.columns}) != len(self.columns):
            raise InputError("A native column can have only one explicit binding")


@dataclass(frozen=True)
class PackageCell:
    """Native record ordinal, column position, original text and logical value.

    Record ordinals include the header. They are not physical source lines:
    quoted multiline CSV cells can span several lines. Null cells emit nothing.
    """

    resource: str
    path: str
    row: int
    column: int
    name: str
    text: str | None
    value: str | int | Decimal | bool | None
    statements: tuple[tuple[URIRef, URIRef, URIRef | RdfLiteral], ...] = ()


@dataclass(frozen=True)
class PackageProducer:
    """Actual installed framework/runtime identity and supported descriptor regime."""

    framework: str
    python: str
    standard: Literal["v1"] = "v1"


@dataclass(frozen=True)
class PackageExtraction:
    """Finite native package checks and selected semantic checks remain distinct.

    A source exists only after complete, native-valid conversion. Semantic
    findings retain that actual candidate without turning it into verified truth.
    """

    source: PreparedSource | None
    cells: tuple[PackageCell, ...]
    producer: PackageProducer | None
    native_report: Report
    check_reports: tuple[Report, ...]
    report: Report
