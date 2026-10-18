"""Independent application declarations and representative output comparisons."""

from .comparison import compare_applications
from .types import (
    ApplicationDeclarationChange,
    ApplicationField,
    ApplicationOutput,
    ApplicationSampleComparison,
    ApplicationUpgrade,
    ApplicationValueChange,
    ApplicationVersion,
)

__all__ = [
    "ApplicationDeclarationChange",
    "ApplicationField",
    "ApplicationOutput",
    "ApplicationSampleComparison",
    "ApplicationUpgrade",
    "ApplicationValueChange",
    "ApplicationVersion",
    "compare_applications",
]
