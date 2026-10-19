"""Asserted slim assignment preserves fine annotations and all nearest ties."""

from .checking import assign_slim
from .types import (
    SlimAnnotation,
    SlimAssignment,
    SlimAssignmentSet,
    SlimCount,
    SlimMatch,
    SlimTermAssignment,
)

__all__ = [
    "SlimAnnotation",
    "SlimAssignment",
    "SlimAssignmentSet",
    "SlimCount",
    "SlimMatch",
    "SlimTermAssignment",
    "assign_slim",
]
