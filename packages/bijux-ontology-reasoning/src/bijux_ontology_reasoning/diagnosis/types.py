"""Separate maintained consistency and class satisfiability observations."""

from __future__ import annotations

from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.reports import Report

from ..backend.types import BackendCapability, ProfileDocument
from ..documents.types import OwlDocument
from ..profiles.types import LoaderQualification


@dataclass(frozen=True)
class ReasonerIdentity:
    """Actual artifact and native versions, which this release reports differently."""

    name: str
    artifact_version: str
    native_version: str
    ignore_unsupported_datatypes: bool


@dataclass(frozen=True)
class ClassSatisfiability:
    """An actual named-class query, never materialized-triple absence."""

    iri: URIRef
    satisfiable: bool
    report: Report


@dataclass(frozen=True)
class ConsistencyCheck:
    """Qualified answers and bounded raw evidence remain independently inspectable.

    A consistent ontology may contain unsatisfiable classes. An inconsistent
    ontology has no ordinary accepted class satisfiability answers. Partial
    execution retains only completed native answers; aggregate coverage remains
    incomplete. Original sources and loader receipts preserve selected scope.
    """

    consistent: bool | None
    raw_consistent: bool | None
    in_dl_profile: bool | None
    native_error: str | None
    classes: tuple[ClassSatisfiability, ...]
    requested_classes: tuple[URIRef, ...] | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
