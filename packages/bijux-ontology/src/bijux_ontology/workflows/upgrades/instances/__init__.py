"""Explicit instance-data release identity and change contracts."""

from .comparison import compare_instance_releases
from .types import (
    InstanceEntity,
    InstanceEntityPair,
    InstanceFieldChange,
    InstanceFieldPolicy,
    InstanceRelease,
    InstanceReleaseComparison,
    InstanceSharedNodeChange,
)

__all__ = [
    "InstanceEntity",
    "InstanceEntityPair",
    "InstanceFieldChange",
    "InstanceFieldPolicy",
    "InstanceRelease",
    "InstanceReleaseComparison",
    "InstanceSharedNodeChange",
    "compare_instance_releases",
]
