"""Checked plain-label projections and explicit XL resource promotion."""

from .projection import project_xl_labels
from .promotion import promote_labels
from .types import LabelCandidate, LabelCorrespondence, LabelIdentity

__all__ = [
    "LabelCandidate",
    "LabelCorrespondence",
    "LabelIdentity",
    "project_xl_labels",
    "promote_labels",
]
