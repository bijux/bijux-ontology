"""Raw-token observations and explicitly requested typed numeric projections."""

import json
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.json.paths import SourcePath, validate_path

NumberPolicy = Literal["processor", "portable-integer", "source-exact"]


@dataclass(frozen=True)
class NumericProjection:
    """Replace one explicitly selected raw numeric cell with a typed value object."""

    path: SourcePath
    datatype: str

    def __post_init__(self) -> None:
        """Own finite concrete paths without interpreting JSON-LD context grammar."""
        validate_path(self.path)
        if not isinstance(self.datatype, str):
            raise InputError("Numeric projection requires explicit datatype text")
        object.__setattr__(self, "datatype", str(self.datatype))


@dataclass(frozen=True)
class NumericObservation:
    """Original token/path beside independent decoder and RDF-route exactness."""

    path: SourcePath
    token: str
    decoded_kind: str
    decoded_exact: bool | None
    rdf_exact: bool | None = None
    projected_datatype: str | None = None


@dataclass(frozen=True)
class NumericJSON:
    """Owned ordinary JSON is published only when its selected contract verifies."""

    document_json: str | None
    observations: tuple[NumericObservation, ...]
    report: Report

    @property
    def document(self) -> object:
        """Return an independent document without manufacturing raw-token provenance."""
        return (
            json.loads(self.document_json) if self.document_json is not None else None
        )
