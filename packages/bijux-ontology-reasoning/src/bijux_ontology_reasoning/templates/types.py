"""Explicit native table-template bindings and qualified row/output receipts."""

from dataclasses import dataclass
from pathlib import Path

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.stores.seals import StoreFile
from bijux_ontology.reports import Report

from ..axioms.types import AxiomInspection, NamedEntity
from ..documents.types import NativeExport, OwlDocument
from ..entailment.types import EntailmentCheck


@dataclass(frozen=True, init=False)
class TemplateBackend:
    """One caller-pinned standalone ROBOT release, with no ambient installation."""

    java: Path
    jar: Path
    seal: StoreFile
    artifacts: Path

    def __init__(self) -> None:
        """Require qualification of the actual maintained runtime file."""
        raise InputError("Use prepare_template_backend")

    @classmethod
    def _owned(cls, **values: object) -> "TemplateBackend":
        """Adopt only bounded explicitly qualified immutable runtime metadata."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class TemplateColumn:
    """An exact ordinary header and documented native template instruction."""

    header: str
    template: str
    required: bool = True


@dataclass(frozen=True)
class TemplateIdentifier:
    """An explicit generated-row token and its intended absolute class IRI."""

    token: str
    iri: str


@dataclass(frozen=True)
class TemplateTerm:
    """An exact local token with a declared class/property/annotation role."""

    token: str
    entity: NamedEntity


@dataclass(frozen=True)
class TableRowReceipt:
    """Logical CSV/TSV record number and distinct preparation/output evidence."""

    row: int
    identifier: str | None
    input_report: Report
    output_report: Report | None = None


@dataclass(frozen=True)
class TableCompilation:
    """Original owned inputs and native evidence, with accepted output withheld.

    Only full row accounting, exact supported axiom coverage, selected profiles
    and nonempty consumer obligations can publish output. Inspection/profile
    truth alone is not consistency or application entailment.
    """

    table: bytes | None
    columns: tuple[TemplateColumn, ...]
    bindings: OwlDocument | None
    rows: tuple[TableRowReceipt, ...]
    native_version: str | None
    binding_inspection: AxiomInspection | None
    inspection: AxiomInspection | None
    conversion: NativeExport | None
    obligations: EntailmentCheck | None
    output: OwlDocument | None
    report: Report
