"""Explicit bounded HTTP preparation into ordinary offline project bundles."""

from .preparation import acquire_bundle
from .types import (
    AcquiredResource,
    AcquisitionPolicy,
    AcquisitionRequest,
    BundleAcquisition,
)

__all__ = (
    "AcquisitionPolicy",
    "AcquisitionRequest",
    "AcquiredResource",
    "BundleAcquisition",
    "acquire_bundle",
)
