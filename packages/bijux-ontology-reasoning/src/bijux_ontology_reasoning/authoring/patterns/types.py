"""Owned DOSDP inputs and finite syntactic binding inventories."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.queries import QueryScope
from bijux_ontology.rdf.queries.native.types import EngineAnswer
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.resources.files import FileSnapshot


@dataclass(frozen=True, init=False)
class DosdpQueryBackend:
    """An explicitly selected immutable maintained distribution and executable."""

    java: Path
    directory: Path
    jars: tuple[str, ...]
    files: tuple[FileSnapshot, ...]
    manifest_sha256: str
    artifacts: Path
    version: str

    def __init__(self) -> None:
        """Require bounded local bundle capture rather than ambient discovery."""
        raise InputError("Use prepare_dosdp_backend")

    @classmethod
    def _owned(cls, **values: object) -> "DosdpQueryBackend":
        """Publish the already qualified immutable factory record."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True, init=False)
class DosdpPattern:
    """Original JSON DOSDP bytes, explicit prefixes and qualified named variables.

    JSON is the supported subset of the maintained YAML input contract. The
    native tool, rather than this record, parses the logical pattern expression.
    """

    iri: str
    content: bytes
    prefixes: tuple[tuple[str, str], ...]
    variables: tuple[str, ...]
    sha256: str
    unsupported: tuple[str, ...]

    def __init__(self) -> None:
        """Require source and capability qualification before native execution."""
        raise InputError("Use prepare_dosdp_pattern")

    @classmethod
    def _owned(cls, **values: object) -> "DosdpPattern":
        """Retain immutable values captured by the profile factory."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class PatternFiller:
    """One exact named variable and original named RDF filler."""

    variable: str
    value: URIRef


@dataclass(frozen=True)
class PatternInstance:
    """A complete assignment; multiple assignments for one class stay distinct."""

    pattern_iri: str
    defined_class: URIRef
    fillers: tuple[PatternFiller, ...]


@dataclass(frozen=True)
class PatternQuery:
    """Actual generated SPARQL beside its single maintained engine observation."""

    pattern: DosdpPattern
    query: str | None
    native_version: str
    native_identity: str | None
    matches: tuple[PatternInstance, ...]
    engine: EngineAnswer | None
    report: Report


@dataclass(frozen=True)
class PatternCoverage:
    """Scoped expected-inventory truth, never general logical nonconformance."""

    scope: QueryScope
    source_metadata: SnapshotMetadata | None
    import_metadata: tuple[SnapshotMetadata, ...]
    matching: Literal["syntactic", "inferred"]
    queries: tuple[PatternQuery, ...]
    report: Report
