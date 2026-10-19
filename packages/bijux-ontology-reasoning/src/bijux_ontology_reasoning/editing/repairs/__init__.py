"""Bounded explanation-led repairs with explicit consumer and ownership controls."""

from .search import propose_repairs
from .types import (
    AllowedReplacement,
    RepairEdit,
    RepairProposal,
    RepairSearch,
)

__all__ = [
    "AllowedReplacement",
    "RepairEdit",
    "RepairProposal",
    "RepairSearch",
    "propose_repairs",
]
