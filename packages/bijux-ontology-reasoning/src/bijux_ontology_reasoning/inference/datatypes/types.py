"""Scoped datatype obligations and actual maintained satisfiability evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.declarations.types import FieldDeclaration
from bijux_ontology.reports import Report

from ...axioms.types import DataRange, PropertyDomainRange
from ...backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ...documents.types import OwlDocument


@dataclass(frozen=True)
class DatatypeObligation:
    """A universal range relationship, independent of finite sample validity."""

    actual: DataRange
    required: DataRange
    relation: Literal["inclusion", "disjointness"] = "inclusion"
    location: str = ""


@dataclass(frozen=True)
class DatatypeFieldBranch:
    """An original branch checked against selected global data-property ranges.

    Context-specific restrictions and model co-emission are separate operations.
    """

    field: FieldDeclaration
    branch: int


DatatypeRequest = DatatypeObligation | DatatypeFieldBranch
DatatypeOutcome = Literal[
    "included", "not_included", "disjoint", "not_disjoint", "overlap", "unknown"
]


@dataclass(frozen=True)
class DatatypeAnswer:
    """A request with exact native reductions and scoped property-range witnesses."""

    request: DatatypeRequest
    outcome: DatatypeOutcome
    actual: DataRange | None
    required: DataRange | None
    ranges: tuple[PropertyDomainRange, ...]
    raw_difference_satisfiable: bool | None
    raw_intersection_satisfiable: bool | None
    native_error: str | None
    report: Report


@dataclass(frozen=True)
class DatatypeCheck:
    """Finite coverage under original and declared-fresh temporary consistency."""

    answers: tuple[DatatypeAnswer, ...]
    consistent: bool | None
    raw_consistent: bool | None
    temporary_consistent: bool | None
    fresh_property: str | None
    fresh_absent: bool | None
    in_dl_profile: bool | None
    native_error: str | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
