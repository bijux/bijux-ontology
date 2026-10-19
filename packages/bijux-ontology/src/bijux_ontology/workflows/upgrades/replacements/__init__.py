"""Explicit role-qualified replacement proposals with required consumer rechecks."""

from .planning import plan_replacements
from .types import (
    ApprovedReplacement,
    ReplacementCheck,
    ReplacementDecision,
    ReplacementPlan,
    ReplacementSite,
    ReplacementState,
)

__all__ = [
    "ApprovedReplacement",
    "ReplacementCheck",
    "ReplacementDecision",
    "ReplacementPlan",
    "ReplacementSite",
    "ReplacementState",
    "plan_replacements",
]
