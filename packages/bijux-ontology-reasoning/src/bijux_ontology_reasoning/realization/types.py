"""Named individual consequences with explicit native grouping and value scope."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report

from ..axioms.types import DataProperty, NamedEntity, NativeLiteral, ObjectProperty
from ..backend.types import (
    BackendCapability,
    LoaderQualification,
    ProfileDocument,
    ReasonerIdentity,
)
from ..documents.types import OwlDocument

IndividualFamily = Literal[
    "types", "instances", "object_values", "data_values", "same", "different"
]


@dataclass(frozen=True)
class IndividualQuery:
    """One selected family; only types and instances take explicit hierarchy depth."""

    family: IndividualFamily
    focus: NamedEntity
    property: ObjectProperty | DataProperty | None = None
    direct: bool | None = None


@dataclass(frozen=True)
class RealizationGroup:
    """One actual native node, interpreted under the returned individual node policy.

    BY_NAME individual nodes are singleton name groups, not maximal sameAs
    equivalence classes. Only the separately selected same query returns the
    reasoner's same-individual node. Class nodes retain class equivalence.
    """

    members: tuple[NamedEntity, ...]
    complete: bool


@dataclass(frozen=True)
class IndividualAnswer:
    """One independently qualified family; absent values never assert negation.

    Data values cover logical values represented by supplied positive assertion
    literals in the declared assertion/subproperty/equivalent-property fragment.
    They do not enumerate every RDF lexical spelling of a logical value.
    """

    query: IndividualQuery
    types: tuple[RealizationGroup, ...]
    instances: tuple[RealizationGroup, ...]
    object_values: tuple[RealizationGroup, ...]
    data_values: tuple[NativeLiteral, ...]
    same_individuals: RealizationGroup | None
    different_individuals: tuple[RealizationGroup, ...]
    node_policy: str | None
    native_error: str | None
    report: Report


@dataclass(frozen=True)
class IndividualRealization:
    """Named consequences over the exact bound, consistent ontology plus ABox."""

    answers: tuple[IndividualAnswer, ...]
    consistent: bool | None
    theory_identity: str | None
    documents: tuple[OwlDocument | ProfileDocument, ...]
    rdf_sources: tuple[PreparedSource, ...]
    loaders: tuple[LoaderQualification, ...]
    reasoner: ReasonerIdentity | None
    capability: BackendCapability | None
    consistency_report: Report
    report: Report
