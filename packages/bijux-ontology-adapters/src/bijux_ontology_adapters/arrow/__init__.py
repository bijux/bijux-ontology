"""Optional finite Arrow records with explicit RDF semantics and owned cursors."""

from .preparation import prepare_arrow_contract
from .stream import ArrowRecordStream, open_arrow_records
from .types import (
    ArrowBatchResult,
    ArrowCell,
    ArrowColumn,
    ArrowContract,
    ArrowRowResult,
)

__all__ = [
    "ArrowBatchResult",
    "ArrowCell",
    "ArrowColumn",
    "ArrowContract",
    "ArrowRecordStream",
    "ArrowRowResult",
    "open_arrow_records",
    "prepare_arrow_contract",
]
