"""Explicit finite module interfaces over existing asserted semantic proofs."""

from .checking import check_module_wiring
from .types import (
    ModuleBinding,
    ModuleContract,
    ModulePropertyRequirement,
    ModuleProvider,
    ModuleRequirement,
    ModuleRequirementResult,
    ModuleWiring,
)

__all__ = [
    "ModuleBinding",
    "ModuleContract",
    "ModulePropertyRequirement",
    "ModuleProvider",
    "ModuleRequirement",
    "ModuleRequirementResult",
    "ModuleWiring",
    "check_module_wiring",
]
