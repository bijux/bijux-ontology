"""Explicit native OBO-in-OWL definition and synonym record inspection."""

from .reading import read_obo_records
from .types import OboAttachment, OboRecord, OboRecords

__all__ = ["OboAttachment", "OboRecord", "OboRecords", "read_obo_records"]
