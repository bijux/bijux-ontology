"""Loss-aware external SHACL report ingestion and separate-scope aggregation."""

from .aggregation import aggregate_shacl_reports
from .ingestion import ingest_external_report
from .types import ExternalShaclReport, ExternalShaclScope, ShaclReportAggregate

__all__ = [
    "ExternalShaclReport",
    "ExternalShaclScope",
    "ShaclReportAggregate",
    "aggregate_shacl_reports",
    "ingest_external_report",
]
