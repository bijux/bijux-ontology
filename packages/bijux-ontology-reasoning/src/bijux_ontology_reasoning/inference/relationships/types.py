"""Explicit native relationship queries and qualified finite enumeration evidence."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from bijux_ontology.reports import Report

from ...axioms.types import DataProperty, NamedEntity, ObjectProperty
from ...backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ...documents.types import OwlDocument

RelationshipValue: TypeAlias = NamedEntity | ObjectProperty | DataProperty
RelationshipKind = Literal["super", "sub", "equivalent", "inverse", "domain", "range"]


@dataclass(frozen=True)
class InferredQuery:
    """One named-class/property query; hierarchy depth is explicitly selected."""

    focus: RelationshipValue
    relation: RelationshipKind
    direct: bool | None = None


@dataclass(frozen=True)
class RelationshipMember:
    """One reasoner-returned relation and its direct native asserted-axiom owners.

    An empty asserted inventory means no matching structural assertion was
    present in the selected closure; it never means an entailed negation.
    """

    value: RelationshipValue
    asserted_in: tuple[OwlDocument | ProfileDocument, ...]


@dataclass(frozen=True)
class RelationshipGroup:
    """A native equivalence node, retaining partial membership honestly."""

    members: tuple[RelationshipMember, ...]
    top: bool
    bottom: bool
    complete: bool


@dataclass(frozen=True)
class InferredAnswer:
    """Finite enumeration truth, separate from the queried class's satisfiability."""

    query: InferredQuery
    groups: tuple[RelationshipGroup, ...]
    satisfiable: bool | None
    native_error: str | None
    report: Report


@dataclass(frozen=True)
class InferredContracts:
    """Named relationships over the exact qualified closure, including native builtins.

    Completeness covers only each selected native named-signature query, never
    every possible class expression. A verified enumeration with satisfiable=False
    reports an actual unsatisfiable class; it does not claim that class is usable.
    """

    answers: tuple[InferredAnswer, ...]
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
