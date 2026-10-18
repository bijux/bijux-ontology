"""Explicit native OBO-in-OWL definition and synonym record inspection."""

from .identifiers import (
    AlternativeIdCandidate,
    AlternativeIdResolution,
    resolve_alternative_ids,
)
from .reading import read_obo_records
from .types import OboAttachment, OboRecord, OboRecords

__all__ = [
    "AlternativeIdCandidate",
    "AlternativeIdResolution",
    "OboAttachment",
    "OboRecord",
    "OboRecords",
    "read_obo_records",
    "resolve_alternative_ids",
]
