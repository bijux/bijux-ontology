"""Selected OWL profile answers and original document/loader qualification."""

from __future__ import annotations

from dataclasses import dataclass

from bijux_ontology.reports import Report

from ..backend.types import BackendCapability as BackendCapability
from ..backend.types import LoaderQualification as LoaderQualification
from ..backend.types import OwlProfile as OwlProfile
from ..backend.types import OwlProfileBackend as OwlProfileBackend
from ..backend.types import ProfileDocument as ProfileDocument


@dataclass(frozen=True)
class ProfileViolation:
    """A real maintained violation with owning document and diagnostic rendering."""

    kind: str
    document: ProfileDocument
    axiom: str | None
    expression: str | None


@dataclass(frozen=True)
class ProfileCheck:
    """Selected OWL profile membership, never consistency or satisfiability.

    raw_in_profile retains the maintained answer even when loader qualification
    prevents accepting it for the supplied RDF. A qualified false answer is a
    finding; unavailable/partial parsing or execution remains unknown.
    """

    profile: OwlProfile
    capability: BackendCapability | None
    in_profile: bool | None
    raw_in_profile: bool | None
    violations: tuple[ProfileViolation, ...]
    documents: tuple[ProfileDocument, ...]
    loaders: tuple[LoaderQualification, ...]
    report: Report
