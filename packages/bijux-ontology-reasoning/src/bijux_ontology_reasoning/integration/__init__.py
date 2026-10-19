"""Selected joint-theory compatibility with original and bridge baselines."""

from .checking import check_integration
from .types import (
    EntailmentComparison,
    IntegrationCheck,
    JointCompatibility,
    TheoryCombination,
    TheoryComparison,
    TheoryComponent,
    TheoryInteraction,
    TheoryObservation,
)

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
