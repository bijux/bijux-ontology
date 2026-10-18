"""Maintained framing with explicit retained and permitted projection scope."""

from .checking import frame_document
from .types import FramedDocument, FrameNode, FrameOmission

__all__ = ["FrameNode", "FrameOmission", "FramedDocument", "frame_document"]
