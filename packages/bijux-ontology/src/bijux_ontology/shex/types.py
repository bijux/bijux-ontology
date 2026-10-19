"""Explicit supplied ShEx schemas, RDF shape-map obligations and native results."""

import json
from dataclasses import dataclass
from typing import Literal

from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class ShexSchema:
    """Supply native ShapeDecl ShExJ text under its exact logical identity."""

    document_iri: str
    content: str


@dataclass(frozen=True)
class ShexBinding:
    """Request one RDF node against one authored shape; no implicit discovery."""

    focus: Node
    shape: Node


@dataclass(frozen=True)
class ShexObligation:
    """Retain one requested identity and its actual native result when completed."""

    binding: ShexBinding
    status: Literal["matched", "not_matched", "unknown"]
    native_json: str | None = None

    @property
    def native(self) -> object:
        """Export independent structured engine diagnostics without mutable aliases."""
        return json.loads(self.native_json) if self.native_json is not None else None


@dataclass(frozen=True)
class ShexResult:
    """Keep exact required coverage separate from native observations and scope."""

    obligations: tuple[ShexObligation, ...]
    report: Report
    schema_iri: str = ""
    imports: tuple[str, ...] = ()
    graph_scope: str = ""
    backend_version: str = ""
    diagnostics_json: str | None = None

    @property
    def diagnostics(self) -> object:
        """Expose native load/compile exceptions separately from validation results."""
        return (
            json.loads(self.diagnostics_json)
            if self.diagnostics_json is not None
            else None
        )
