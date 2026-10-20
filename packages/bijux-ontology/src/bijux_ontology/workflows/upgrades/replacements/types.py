"""Explicit replacement intent and reviewable original-position decisions."""

from collections.abc import Callable
from dataclasses import dataclass

from rdflib import URIRef

from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.records.types import PatchQuad, SnapshotWitness
from bijux_ontology.rdf.roles.types import TermPosition, TermRole
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.workflows.upgrades.uses import Use


@dataclass(frozen=True)
class ReplacementSite:
    """
    Original declaration use or exact quad position and authorized discriminator data.
    """

    iri: URIRef
    role: TermRole
    snapshot: SnapshotWitness
    use: Use | None = None
    quad: PatchQuad | None = None
    position: TermPosition | None = None


@dataclass(frozen=True)
class ApprovedReplacement:
    """Caller authority, not equivalence inferred from an ontology's hints.

    Splits invoke an ordinary trusted in-process discriminator on original facts.
    Its execution cannot be preempted; no result is cached across operations.
    """

    sources: tuple[URIRef, ...]
    targets: tuple[URIRef, ...]
    kind: TermRole
    owner: str
    merge: bool = False
    choose: Callable[[ReplacementSite], URIRef | tuple[URIRef, ...] | None] | None = (
        None
    )

    def __post_init__(self) -> None:
        """
        Require bounded immutable named inventories and finite role/intent choices.
        """
        budget = Budget()
        for values in (self.sources, self.targets):
            if type(values) is not tuple or not values:
                raise InputError(
                    "Replacement intent requires nonempty immutable IRI tuples"
                )
            budget.consume("states", len(values))
            for iri in values:
                if not isinstance(iri, URIRef):
                    raise InputError("Replacement endpoints require URIRef values")
                bound_text(iri, budget)
                absolute_iri(iri)
            if len(set(values)) != len(values):
                raise InputError("Replacement endpoints cannot repeat")
        if len(self.sources) > 1 and len(self.targets) > 1:
            raise InputError("Many-to-many replacement intent is unsupported")
        bound_text(self.kind, budget)
        if self.kind not in (
            "class",
            "concept",
            "individual",
            "property",
            "object_property",
            "datatype_property",
            "annotation_property",
            "datatype",
        ):
            raise InputError("Select an explicit replaceable entity role")
        bound_text(self.owner, budget)
        if not self.owner or type(self.merge) is not bool:
            raise InputError("Replacement requires an owner and boolean merge intent")
        if self.choose is not None and not callable(self.choose):
            raise InputError("A replacement discriminator must be callable")


@dataclass(frozen=True)
class ReplacementDecision:
    """One selected target attached to its established original semantic position."""

    site: ReplacementSite
    target: URIRef


@dataclass(frozen=True)
class ReplacementState:
    """A declaration inventory and the exact authorized owned RDF checked with it."""

    terms: tuple[TermDeclaration, ...]
    models: tuple[ModelDeclaration, ...]
    source: PreparedSource


@dataclass(frozen=True)
class ReplacementCheck:
    """A required ordinary consumer check receiving the actual updated contract/data."""

    name: str
    check: Callable[[ReplacementState], Report]

    def __post_init__(self) -> None:
        """Validate metadata without executing caller code."""
        bound_text(self.name, Budget())
        if not self.name or not callable(self.check):
            raise InputError("Replacement checks require a name and callable")


@dataclass(frozen=True)
class ReplacementPlan:
    """Candidate availability remains distinct from explicit migration acceptance."""

    before: ReplacementState | None
    after: ReplacementState | None
    decisions: tuple[ReplacementDecision, ...]
    candidate: MigrationCandidate
    report: Report
