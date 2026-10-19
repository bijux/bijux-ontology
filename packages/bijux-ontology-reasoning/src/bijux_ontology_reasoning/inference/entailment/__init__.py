"""Typed supported DL entailment obligations through the explicit owned backend."""

from .checking import check_entailments
from .types import (
    EntailmentAnswer,
    EntailmentCheck,
    EntailmentOutcome,
)

__all__ = [
    "EntailmentAnswer",
    "EntailmentCheck",
    "EntailmentOutcome",
    "check_entailments",
]
