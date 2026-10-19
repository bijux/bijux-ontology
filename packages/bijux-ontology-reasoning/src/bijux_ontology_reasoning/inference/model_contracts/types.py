"""Separate model entailment, possibility and co-emission questions."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report

from ...axioms.types import ClassExpression
from ...backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ...documents.types import OwlDocument
from ..datatypes.types import DatatypeCheck

ModelQuestionKind = Literal[
    "required_subsumption", "possible_satisfiability", "coemission_contradiction"
]


@dataclass(frozen=True)
class ModelQuestion:
    """One exact declaration occurrence and finite state, without inferred fields."""

    model: str
    kind: ModelQuestionKind
    location: str
    field: str | None = None
    branch: int | None = None
    present: tuple[str, ...] = ()
    covered: bool = True
    guaranteed: bool = True


@dataclass(frozen=True)
class ModelLogicalAnswer:
    """Native raw question outcomes; possibility never substitutes for entailment."""

    question: ModelQuestion
    actual: ClassExpression | None
    required: ClassExpression | None
    raw_entailed: bool | None
    raw_satisfiable: bool | None
    native_error: str | None
    report: Report


@dataclass(frozen=True)
class ModelCompatibility:
    """Selected logical compatibility, separate from source/output conformance.

    Native existence and cardinality do not prove emitted required fields. Use
    ordinary output checking for those obligations. Unsupported declarations or
    contexts remain required unknown; no raw finite-model report is rewritten.
    """

    models: tuple[ModelDeclaration, ...]
    answers: tuple[ModelLogicalAnswer, ...]
    datatypes: tuple[DatatypeCheck, ...]
    consistent: bool | None
    raw_consistent: bool | None
    in_dl_profile: bool | None
    native_error: str | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    rdf_sources: tuple[PreparedSource, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
