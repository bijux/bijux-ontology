"""Sealed semantic preparation and revision-bound operation receipts."""

from _thread import LockType
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.models.persistence.types import (
    ModelPreparationArtifact,
    PreparationIdentity,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.execution.results import ShaclResult


@dataclass(frozen=True)
class ShapePreparationProbe:
    """Actual immutable positive data and named focus terms for selected shapes."""

    source: RdfSource
    focus_nodes: tuple[str, ...]


@dataclass(frozen=True)
class RevisionResult:
    """Fresh actual-output evidence bound to the revision that ran the check."""

    revision: str
    report: Report
    shacl: ShaclResult | None = None


@dataclass(frozen=True)
class RevisionPreparation:
    """Only complete required preparation can yield an activatable revision."""

    prepared: "PreparedRevision | None"
    report: Report


@dataclass(frozen=True)
class RevisionStatus:
    """Bounded manager state, without exposing shared semantic input owners."""

    active: str | None
    retained: tuple[str, ...]
    leases: int
    operations: int
    retained_bytes: int
    closing: bool
    closed: bool


@dataclass(frozen=True)
class _RevisionState:
    """Existing immutable semantic owners transferred once into one manager."""

    revision: str
    identity: PreparationIdentity
    artifact: ModelPreparationArtifact
    models: PreparedModels
    ontology: PreparedSource | None
    shapes: PreparedSource | None
    imports: Mapping[str, RdfSource]
    roots: tuple[str, ...]
    report: Report
    retained_bytes: int


class PreparedRevision:
    """Factory-owned preparation with explicit single-owner transfer or close.

    Metadata remains inspectable after transfer. Shared models and RDF inputs
    are accessible only through the manager's leased operations. Close releases
    unadopted preparation; it cannot retire a manager's live semantic owners.
    """

    __slots__ = (
        "_lock",
        "_state",
        "_transferred",
        "_revision",
        "_report",
        "_retained_bytes",
        "_identity",
    )

    _lock: LockType
    _state: _RevisionState | None
    _transferred: bool
    _revision: str
    _report: Report
    _retained_bytes: int
    _identity: PreparationIdentity

    def __init__(self) -> None:
        """Require checked preparation rather than caller-assembled state."""
        raise InputError("Use prepare_revision to create a checked semantic revision")

    @classmethod
    def _owned(cls, state: _RevisionState) -> "PreparedRevision":
        """Retain only one privately prepared set of semantic input owners."""
        result = object.__new__(cls)
        result._lock = Lock()
        result._state = state
        result._transferred = False
        result._revision = state.revision
        result._report = state.report
        result._retained_bytes = state.retained_bytes
        result._identity = state.identity
        return result

    @property
    def revision(self) -> str:
        """Identify this exact prepared resource/contract revision."""
        return self._revision

    @property
    def identity(self) -> PreparationIdentity:
        """Bind exact closed resources, selections, required probes and engines."""
        return self._identity

    @property
    def report(self) -> Report:
        """Inspect actual required preparation, independently of activation."""
        return self._report

    @property
    def retained_bytes(self) -> int:
        """Expose conservative paid preparation/adoption storage, not process RSS."""
        return self._retained_bytes

    def _peek(self) -> _RevisionState:
        """Inspect a still-owned immutable state before an atomic capacity check."""
        with self._lock:
            if self._state is None or self._transferred:
                raise InputError("Prepared revision is already transferred or closed")
            return self._state

    def _claim(self) -> _RevisionState:
        """Transfer exactly once, without allowing two managers to share ownership."""
        with self._lock:
            if self._state is None or self._transferred:
                raise InputError("Prepared revision is already transferred or closed")
            state = self._state
            self._state = None
            self._transferred = True
            return state

    def close(self) -> None:
        """Release unused preparation without affecting an adopted revision."""
        with self._lock:
            if self._transferred:
                raise InputError("Retire adopted revisions through their manager")
            self._state = None

    def __enter__(self) -> "PreparedRevision":
        """Own unadopted preparation for a bounded application scope."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release unused preparation; adopted lifetime belongs to its manager."""
        with self._lock:
            if not self._transferred:
                self._state = None
