"""Owned persistent RDF identity, independent of mutable native store handles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report

from .seals import StoreFile

if TYPE_CHECKING:
    from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True, init=False)
class StoredRdf:
    """A closed database revision; no mutable native store object escapes."""

    directory: Path
    files: tuple[StoreFile, ...]
    source_sha256: str
    source_format: str
    quads: int
    graph_names: tuple[URIRef | BNode, ...]
    graph_fidelity: Literal["complete", "nonempty-only"]
    backend_version: str
    revision: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise InputError("Use snapshot_rdf or open_snapshot for an owned RDF store")

    @classmethod
    def _owned(cls, **values: object) -> StoredRdf:
        """Adopt only fields qualified by the persistent snapshot protocol."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class StoredRdfResult:
    """A persistent read-only resource beside actual completion evidence."""

    snapshot: StoredRdf | None
    report: Report


@dataclass(frozen=True)
class MaterializedRdf:
    """An explicitly bounded in-memory projection, separate from the disk store."""

    source: PreparedSource | None
    report: Report
