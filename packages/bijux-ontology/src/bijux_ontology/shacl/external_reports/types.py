"""Externally asserted SHACL evidence with distinct report-local RDF ownership."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Graph
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.shacl.findings import ShaclFinding

if TYPE_CHECKING:
    from bijux_ontology.shacl.execution.results import ShaclResult


@dataclass(frozen=True)
class ExternalShaclScope:
    """Descriptive producer claims, never independently established local proof.

    Values identify the claimed data, shapes and execution selection/regime.
    Missing values remain visibly missing; names are not integrity receipts.
    """

    data: str | None = None
    shapes: str | None = None
    execution: str | None = None


@dataclass(frozen=True, init=False)
class ExternalShaclReport:
    """Owned imported RDF beside raw external conformance and scoped findings.

    Instances are produced by ingestion. Findings and their blank nodes belong
    only to this report; they do not bind nodes in a current local data resource.
    """

    scope: ExternalShaclScope | None
    backend: str | None
    conforms: bool | None
    report_node: Node | None
    findings: tuple[ShaclFinding, ...]
    root_findings: tuple[ShaclFinding, ...]
    findings_complete: bool
    structural_report: Report
    report: Report
    _source: PreparedSource | None

    def __init__(self) -> None:
        """Require the bounded report ingestion boundary."""
        raise InputError("External SHACL reports require ingest_external_report")

    def report_graph(self) -> Graph:
        """Return an independent complete raw graph, including unknown extensions."""
        return self._source.graph() if self._source is not None else Graph()


@dataclass(frozen=True, init=False)
class ShaclReportAggregate:
    """Separate report scopes beside their transitive required aggregate truth."""

    results: tuple[ExternalShaclReport | ShaclResult, ...]
    report: Report

    def __init__(self) -> None:
        """Require the bounded typed aggregation boundary."""
        raise InputError("SHACL report aggregates require aggregate_shacl_reports")
