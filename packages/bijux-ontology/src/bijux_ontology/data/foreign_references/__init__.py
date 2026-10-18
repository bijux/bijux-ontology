"""Business-key joins across explicitly supplied scoped record corpora."""

from .checking import check_foreign_references
from .preparation import prepare_foreign_targets
from .types import (
    ForeignReferenceContract,
    ForeignReferenceResult,
    ForeignResolution,
    ForeignTargetIndex,
)

__all__ = [
    "ForeignReferenceContract",
    "ForeignReferenceResult",
    "ForeignResolution",
    "ForeignTargetIndex",
    "check_foreign_references",
    "prepare_foreign_targets",
]
