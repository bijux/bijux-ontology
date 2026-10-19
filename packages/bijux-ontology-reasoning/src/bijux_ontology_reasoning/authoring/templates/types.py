"""Explicit native table-template bindings and qualified row/output receipts."""

from dataclasses import dataclass

from bijux_ontology.reports import Report

from ...axioms.types import AxiomInspection, NamedEntity
from ...backend.robot.types import RobotBackend
from ...documents.types import NativeExport, OwlDocument
from ...inference.entailment.types import EntailmentCheck

TemplateBackend = RobotBackend


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
