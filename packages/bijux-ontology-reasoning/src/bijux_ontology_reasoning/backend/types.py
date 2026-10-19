"""Pinned native backend identities shared by explicit OWL operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.resources.files import FileSnapshot

OwlProfile = Literal["owl2-dl", "owl2-el", "owl2-ql", "owl2-rl"]


@dataclass(frozen=True, init=False)
class OwlProfileBackend:
    """An explicit Java executable and caller-pinned immutable flat worker bundle."""

    java: Path
    directory: Path
    manifest_sha256: str
    jars: tuple[str, ...]
    files: tuple[FileSnapshot, ...]
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


@dataclass(frozen=True)
class ProfileDocument:
    """One selected original RDF document; serialization is not a raw-input hash."""

    identifier: str
    document_iri: str | None
    effective_url: str | None
    role: str
    graph_kind: str
    graph_name: URIRef | BNode | None
    serialization_sha256: str


@dataclass(frozen=True)
class ReasonerIdentity:
    """Actual artifact and native versions, which this release reports differently."""

    name: str
    artifact_version: str
    native_version: str
    ignore_unsupported_datatypes: bool


@dataclass(frozen=True)
class LoaderQualification:
    """Typed OWLAPI parser metadata; absence or guesses do not prove faithful input."""

    document: ProfileDocument
    present: bool
    unparsed: int | None
    guessed: int | None
    triples: int | None
    header: str | None

    @property
    def qualified(self) -> bool:
        """Require actual complete metadata and no unparsed/guessed statements."""
        return self.present and self.unparsed == 0 and self.guessed == 0
