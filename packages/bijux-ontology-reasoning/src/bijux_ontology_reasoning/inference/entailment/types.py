"""Typed requested axioms and distinct maintained entailment observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.reports import Report

from ...axioms.types import ProjectedAxiom
from ...backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ...documents.types import OwlDocument

EntailmentOutcome = Literal["entailed", "not_entailed", "unknown"]


@dataclass(frozen=True)
class EntailmentAnswer:
    """One original obligation; a negative class assertion stays explicit in it.

    A not_entailed answer never implies entailment of an axiom's negation.
    Unknown includes unsupported work and unmet consistency preconditions.
    """

    axiom: ProjectedAxiom
    outcome: EntailmentOutcome
    raw_entailed: bool | None
    native_error: str | None
    report: Report


@dataclass(frozen=True)
class EntailmentCheck:
    """Finite query coverage with an independently qualified consistency result."""

    answers: tuple[EntailmentAnswer, ...]
    consistent: bool | None
    raw_consistent: bool | None
    in_dl_profile: bool | None
    native_error: str | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
