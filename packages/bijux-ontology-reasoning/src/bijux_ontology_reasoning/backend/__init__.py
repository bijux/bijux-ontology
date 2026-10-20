"""Explicit pinned native backend; importing starts no JVM or downloader."""

from .owlapi.bundles import prepare_profile_backend
from .owlapi.execution import backend_capability
from .types import (
    BackendCapability,
    LoaderQualification,
    OwlProfile,
    OwlProfileBackend,
    ProfileDocument,
    ReasonerIdentity,
)

__all__ = [
    "BackendCapability",
    "LoaderQualification",
    "ProfileDocument",
    "ReasonerIdentity",
    "OwlProfile",
    "OwlProfileBackend",
    "backend_capability",
    "prepare_profile_backend",
]
