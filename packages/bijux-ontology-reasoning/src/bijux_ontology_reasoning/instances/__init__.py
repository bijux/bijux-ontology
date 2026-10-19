"""Optional global logical diagnosis of selected actual emitted RDF."""

from ..inference.instances.checking import check_emitted_consistency
from ..inference.instances.types import EmittedConsistency, EmittedRecord, EmittedScope

__all__ = [
    "EmittedConsistency",
    "EmittedRecord",
    "EmittedScope",
    "check_emitted_consistency",
]
