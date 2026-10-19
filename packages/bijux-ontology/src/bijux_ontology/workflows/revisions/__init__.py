"""Atomic activation of owned model/context/shape revisions for embedded Python."""

from .management import RevisionLease, RevisionManager
from .preparation import prepare_revision
from .types import (
    PreparedRevision,
    RevisionPreparation,
    RevisionResult,
    RevisionStatus,
    ShapePreparationProbe,
)

__all__ = [
    "PreparedRevision",
    "RevisionLease",
    "RevisionManager",
    "RevisionPreparation",
    "RevisionResult",
    "RevisionStatus",
    "ShapePreparationProbe",
    "prepare_revision",
]
