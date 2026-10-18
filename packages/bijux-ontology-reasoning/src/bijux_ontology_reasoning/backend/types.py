"""Pinned native backend identities shared by explicit OWL operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.stores.seals import StoreFile
from bijux_ontology.reports import Report

OwlProfile = Literal["owl2-dl", "owl2-el", "owl2-ql", "owl2-rl"]


@dataclass(frozen=True, init=False)
class OwlProfileBackend:
    """An explicit Java executable and caller-pinned immutable flat worker bundle."""

    java: Path
    directory: Path
    manifest_sha256: str
    jars: tuple[str, ...]
    files: tuple[StoreFile, ...]
    artifacts: Path

    def __init__(self) -> None:
        raise InputError("Use prepare_profile_backend with an explicit pinned bundle")

    @classmethod
    def _owned(cls, **values: object) -> OwlProfileBackend:
        """Adopt only bundle fields qualified under the actual operation budget."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class BackendCapability:
    """Actual worker and runtime identity, independent of any ontology answer."""

    owlapi_version: str | None
    java_version: str | None
    vm: str | None
    manifest_sha256: str
    loader_profile: str | None
    profiles: tuple[OwlProfile, ...]
    report: Report
