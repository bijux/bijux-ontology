"""Explicit optional runtime and document-level RDFa producer evidence."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True)
class RdfaRuntime:
    """A caller-selected Node executable and normally installed locked npm tree."""

    executable: str
    directory: Path

    def __post_init__(self) -> None:
        """Keep executable selection explicit and never interpret a shell command."""
        if type(self.executable) is not str or not self.executable:
            raise InputError("RDFa requires an explicit Node executable")
        if not isinstance(self.directory, Path):
            raise InputError("RDFa runtime directory requires a Path")


@dataclass(frozen=True)
class RdfaProducer:
    """Actual native versions, without implying qualification of other hosts."""

    node: str
    parser: str
    host: Literal["html5"] = "html5"


@dataclass(frozen=True)
class RdfaExtraction:
    """Owned asserted RDF and honest source association, separate from JSON-LD."""

    document_url: str
    source: PreparedSource | None
    producer: RdfaProducer | None
    report: Report
    association_precision: Literal["document"] = "document"
    diagnostics: tuple[str, ...] = ()
