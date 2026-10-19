"""Typed supported DL entailment obligations through the explicit owned backend."""

from ..inference.entailment.checking import check_entailments
from ..inference.entailment.types import (
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
