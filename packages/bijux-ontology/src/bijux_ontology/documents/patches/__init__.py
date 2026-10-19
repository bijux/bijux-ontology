"""Checked source-level JSON-LD edits under explicit application obligations."""

from .checking import apply_document_patch, export_document_patch
from .types import DocumentPatch, RetainedUsage

__all__ = [
    "DocumentPatch",
    "RetainedUsage",
    "apply_document_patch",
    "export_document_patch",
]
