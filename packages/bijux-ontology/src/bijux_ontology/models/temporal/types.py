"""Explicit temporal fields and policies over actual emitted JSON records."""

from dataclasses import dataclass

from rdflib import Literal, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology._jsonpaths import SourcePath, validate_path
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Report


@dataclass(frozen=True)
class TemporalField:
    """A concrete text field and its caller-declared XSD datatype."""

    path: SourcePath
    datatype: URIRef

    def __post_init__(self) -> None:
        """Require a concrete selector and preserve the explicit datatype term."""
        validate_path(self.path)
        if not isinstance(self.datatype, URIRef):
            raise InputError("Temporal fields require explicit datatype IRI terms")


@dataclass(frozen=True)
class TemporalInterval:
    """Ordered endpoints, explicit open ends and optional calendar duration bounds.

    Duration bounds are relative to the actual start. A month is never replaced
    with a fixed number of seconds. Missing/null endpoints are open only when
    the corresponding flag explicitly permits them.
    """

    start: TemporalField
    end: TemporalField
    open_start: bool = False
    open_end: bool = False
    minimum_duration: Literal | None = None
    maximum_duration: Literal | None = None


@dataclass(frozen=True)
class TemporalOrder:
    """An explicit predecessor/successor relationship between selected fields."""

    before: TemporalField
    after: TemporalField
    strict: bool = False


@dataclass(frozen=True)
class RelativeTemporal:
    """Require a field before/after an explicitly supplied clock plus an offset."""

    field: TemporalField
    relation: str
    offset: Literal | None = None


@dataclass(frozen=True)
class TemporalObservation:
    """An actual selected field with its unchanged lexical RDF representation."""

    field: TemporalField
    literal: Literal | None
    status: str


@dataclass(frozen=True)
class TemporalCheck:
    """Temporal findings and an independently captured, unchanged input record."""

    report: Report
    observations: tuple[TemporalObservation, ...]
    clock: Literal | None
    _record: bytes | None
    _limits: Limits

    @property
    def document(self) -> object:
        """Return a fresh decoded copy; no calendar value replaces source text."""
        if self._record is None:
            return None
        return decode_json(
            self._record,
            max_bytes=self._limits.max_bytes,
            max_depth=self._limits.max_depth,
        )
