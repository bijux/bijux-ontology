"""Finite reasoner-backed consumer upgrade impact with preserved raw evidence."""

from .checking import assess_logical_upgrade
from .types import (
    EntailmentContract,
    LogicalObservation,
    LogicalSide,
    LogicalSnapshot,
    LogicalTransition,
    LogicalUpgrade,
    ModelContract,
    SatisfiabilityContract,
    SeparationContract,
    UpgradeExplanation,
)

__all__ = [
    "EntailmentContract",
    "LogicalObservation",
    "LogicalSide",
    "LogicalSnapshot",
    "LogicalTransition",
    "LogicalUpgrade",
    "ModelContract",
    "SatisfiabilityContract",
    "SeparationContract",
    "UpgradeExplanation",
    "assess_logical_upgrade",
]
