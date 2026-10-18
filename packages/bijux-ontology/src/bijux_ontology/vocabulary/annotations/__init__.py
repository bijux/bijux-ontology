"""Explicit annotation value and per-language publication coverage."""

from .coverage import check_annotation_coverage
from .types import (
    AnnotationCoverage,
    AnnotationRequirement,
    LanguageCoverage,
    LanguageRequirement,
)

__all__ = [
    "AnnotationCoverage",
    "AnnotationRequirement",
    "LanguageCoverage",
    "LanguageRequirement",
    "check_annotation_coverage",
]
