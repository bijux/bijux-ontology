"""Explicit application record-graph reachability, ownership and cycle checks."""

from .checking import check_record_topology
from .types import TopologyRecord

__all__ = ["TopologyRecord", "check_record_topology"]
