"""Finite source populations beside actual maintained relational RDF output."""

from dataclasses import dataclass
from pathlib import Path

from rdflib import Literal
from rdflib.term import Node

from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceContract,
    ForeignReferenceResult,
)
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.sources.snapshots import PreparedSource


@dataclass(frozen=True, init=False)
class MorphBackend:
    """Explicit separately installed interpreter; no dependency installation."""

    python: Path
    artifacts: Path
    version: str

    def __new__(cls) -> "MorphBackend":
        """Require actual native capability preparation."""
        raise InputError("Use prepare_morph_backend")


@dataclass(frozen=True, init=False)
class SqliteSnapshot:
    """Owned database bytes, original raw integrity and a separate captured hash."""

    content: bytes
    sha256: str
    source_sha256: str
    source_name: str

    def __new__(cls) -> "SqliteSnapshot":
        """Require an actual bounded read-only SQLite snapshot."""
        raise InputError("Use prepare_sqlite_snapshot")


@dataclass(frozen=True)
class R2rmlRow:
    """A caller-selected source key and its intended emitted subject/model."""

    key: Literal
    usage: RdfModelUsage


@dataclass(frozen=True)
class R2rmlPopulation:
    """Full one-map SQL row inventory and exact emitted-key correlation.

    Keys must be unique and non-NULL in the actual selected logical table.
    Expected subjects are unique within this map. No URI template is evaluated
    by this adapter; actual emitted key paths establish the correspondence.
    """

    triples_map: Node
    key_column: str
    emitted_key: KeyContract
    rows: tuple[R2rmlRow, ...]


@dataclass(frozen=True)
class R2rmlForeignCheck:
    """One original referencing object map checked against an independent corpus."""

    object_map: Node
    contract: ForeignReferenceContract
    targets: tuple[KeyRecord, ...]
    target_complete: bool


@dataclass(frozen=True)
class R2rmlJoin:
    """Original mapping nodes and declared join columns, never guessed SQL rows."""

    object_map: Node
    parent_map: Node
    predicate: Node
    child_column: str
    parent_column: str


@dataclass(frozen=True)
class R2rmlTable:
    """Observed logical-table population beside its original mapping identity."""

    triples_map: Node
    logical_table: Node
    table: str
    rows: int
    joins: tuple[R2rmlJoin, ...]
    report: Report


@dataclass(frozen=True)
class R2rmlCheck:
    """Actual native output, complete row/entity obligations and raw child reports."""

    snapshot: SqliteSnapshot | None
    mapping: PreparedSource | None
    output: PreparedSource | None
    nquads: bytes | None
    tables: tuple[R2rmlTable, ...]
    foreign_results: tuple[ForeignReferenceResult, ...]
    model_report: Report | None
    native_report: Report
    report: Report
