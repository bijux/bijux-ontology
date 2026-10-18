"""Explicit optional OWLAPI profile operations; no import-time native execution."""

from .execution import backend_capability, check_profile
from .preparation import prepare_profile_backend
from .types import (
    BackendCapability,
    LoaderQualification,
    OwlProfile,
    OwlProfileBackend,
    ProfileCheck,
    ProfileDocument,
    ProfileViolation,
)

__all__ = [
    "BackendCapability",
    "LoaderQualification",
    "OwlProfile",
    "OwlProfileBackend",
    "ProfileCheck",
    "ProfileDocument",
    "ProfileViolation",
    "backend_capability",
    "check_profile",
    "prepare_profile_backend",
]
