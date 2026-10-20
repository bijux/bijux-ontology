"""Selected joint-theory compatibility with original and bridge baselines."""

from ..types import (
    EntailmentComparison,
    IntegrationCheck,
    JointCompatibility,
    TheoryCombination,
    TheoryComparison,
    TheoryComponent,
    TheoryInteraction,
    TheoryObservation,
)
from .checking import check_integration

__all__ = [
    "EntailmentComparison",
    "IntegrationCheck",
    "JointCompatibility",
    "TheoryCombination",
    "TheoryComparison",
    "TheoryComponent",
    "TheoryInteraction",
    "TheoryObservation",
    "check_integration",
]
