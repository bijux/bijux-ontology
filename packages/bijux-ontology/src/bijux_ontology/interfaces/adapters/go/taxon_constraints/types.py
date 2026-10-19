"""Pinned GO resource roles and finite annotation taxon obligations."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.results import Edge
from bijux_ontology.vocabulary.relation_paths.types import RelationPathResult
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection


def iri(value: URIRef, budget: Budget) -> None:
    """Require actual named RDF terms and qualify them before lookup/hash use."""
    if not isinstance(value, URIRef):
        raise InputError("GO taxon selectors require URIRef terms")
    bound_node(value, budget)
    absolute_iri(value)


@dataclass(frozen=True)
class GoTaxonResource:
    """Immutable local RDF bytes with explicit version, logical base and SHA256.

    Supported RDF syntax is processed by the existing maintained native loader.
    No file, URL acquisition, import resolution or archive submission is implicit.
    A dataset requires one explicit physical graph, never a union projection.
    """

    content: bytes
    format: str
    document_iri: str
    release: str
    sha256: str
    graph_name: str | None = None

    def __post_init__(self) -> None:
        """Keep constructor validation finite; actual checking requalifies fields."""
        if type(self.content) is not bytes:
            raise InputError("GO resource content must be immutable bytes")
        budget = Budget()
        for value in (self.format, self.document_iri, self.release, self.sha256):
            if type(value) is not str or not value:
                raise InputError("GO resources need explicit plain-text identities")
            bound_text(value, budget)
        absolute_iri(self.document_iri)
        if len(self.sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.sha256
        ):
            raise InputError("GO resource pins require lowercase SHA256 digests")
        if self.graph_name is not None:
            bound_text(self.graph_name, budget)
            if self.graph_name != "@default":
                absolute_iri(self.graph_name)


@dataclass(frozen=True)
class GoTaxonRelease:
    """Separate ontology, computed-constraint, grouping and taxonomy documents.

    The modern GO release convention attaches computed constraints directly to
    their constrained terms. Those facts stay computed evidence, never rewritten
    as authored OWL axioms. Independent resources may have distinct version IDs.
    """

    ontology: GoTaxonResource
    computed: GoTaxonResource | None
    groupings: GoTaxonResource | None
    taxonomy: GoTaxonResource | None
    imports: tuple[GoTaxonResource, ...] = ()
    convention: Literal["go-computed-2024-09"] = "go-computed-2024-09"


@dataclass(frozen=True)
class GoTaxonPolicy:
    """Declare selected complete taxonomy ancestry and actual required evidence.

    Completeness is a caller resource-scope declaration, not proof that a release
    describes every biological taxon. Each observed ancestor must be declared and
    connect to an explicit root through supplied named subclass assertions.
    """

    taxonomy_roots: tuple[URIRef, ...]
    complete_ancestry: bool
    require_constraint: bool = True

    def __post_init__(self) -> None:
        """Require immutable finite root selectors and explicit policy booleans."""
        if type(self.taxonomy_roots) is not tuple or not self.taxonomy_roots:
            raise InputError("Select a nonempty explicit taxonomy root tuple")
        budget = Budget()
        budget.consume("states", len(self.taxonomy_roots))
        for root in self.taxonomy_roots:
            iri(root, budget)
        if (
            type(self.complete_ancestry) is not bool
            or type(self.require_constraint) is not bool
        ):
            raise InputError(
                "Taxon completeness/constraint requirements must be booleans"
            )


@dataclass(frozen=True)
class GoTaxonAnnotation:
    """One actual term/taxon annotation occurrence, preserving repeated requests."""

    term: URIRef
    taxon: URIRef
    location: str = ""

    def __post_init__(self) -> None:
        """Validate exact named input intent without inferring a biological scope."""
        budget = Budget()
        iri(self.term, budget)
        iri(self.taxon, budget)
        bound_text(self.location, budget)


@dataclass(frozen=True)
class TaxonConstraint:
    """One independent restriction and original authored/computed source evidence."""

    owner: URIRef
    kind: Literal["only", "never"]
    target: URIRef
    role: Literal["ontology", "computed"]
    evidence: Edge | RestrictionProjection


@dataclass(frozen=True)
class TaxonEvaluation:
    """One applicable clause, exact union alternatives and path/closure evidence."""

    constraint: TaxonConstraint
    alternatives: tuple[URIRef, ...]
    inheritance: RelationPathResult
    ancestry: tuple[RelationPathResult, ...]
    group_structure: tuple[Edge, ...]
    accepted: bool | None
    additional_evidence: tuple[tuple[TaxonConstraint, RelationPathResult], ...] = ()


@dataclass(frozen=True)
class GoTaxonObservation:
    """An annotation's independent conjoined obligations and established result."""

    annotation: GoTaxonAnnotation
    evaluations: tuple[TaxonEvaluation, ...]
    accepted: bool | None


@dataclass(frozen=True)
class GoTaxonReceipt:
    """Exact immutable role/version/byte pin and selected physical RDF syntax/scope."""

    role: str
    document_iri: str
    release: str
    sha256: str
    format: str
    graph_name: str | None


@dataclass(frozen=True)
class GoTaxonCheck:
    """Selected annotation findings plus raw source/version/structural evidence."""

    observations: tuple[GoTaxonObservation, ...]
    resources: tuple[GoTaxonReceipt, ...]
    reports: tuple[Report, ...]
    report: Report
