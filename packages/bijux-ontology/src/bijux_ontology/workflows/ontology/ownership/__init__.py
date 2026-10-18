"""Explicit owner evidence and finite protected-definition reuse policies."""

from .checking import check_ownership
from .types import OwnerClaim, OwnershipCheck, OwnershipEvidence

__all__ = ["OwnerClaim", "OwnershipCheck", "OwnershipEvidence", "check_ownership"]
