"""Exact label-resource correspondence and explicit promotion identities."""

from dataclasses import dataclass

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import SKOS

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.patches.types import PatchQuad, PatchResource
from bijux_ontology.rdf.terms import bound_node, validate_node


@dataclass(frozen=True)
class LabelIdentity:
    """Assign a fresh named XL resource to one exact original plain assertion.

    Distinct assertions require distinct resources, even when their forms agree.
    An owned blank owner is an actual source object, never a parser-label token.
    """

    owner: URIRef | BNode
    predicate: URIRef
    value: Literal
    resource: URIRef

    def __post_init__(self) -> None:
        """Qualify exact RDF kinds before retaining caller-owned terms."""
        if (
            not isinstance(self.owner, URIRef | BNode)
            or not isinstance(self.predicate, URIRef)
            or not isinstance(self.value, Literal)
            or not isinstance(self.resource, URIRef)
        ):
            raise InputError(
                "Label identities require resource, predicate and literal terms"
            )
        budget = Budget()
        for node in (self.owner, self.predicate, self.value, self.resource):
            bound_node(node, budget)
            validate_node(node)
        if self.predicate not in (SKOS.prefLabel, SKOS.altLabel, SKOS.hiddenLabel):
            raise InputError("Label promotion selects a standard plain SKOS label role")


@dataclass(frozen=True)
class LabelCorrespondence:
    """One resource-specific link/form pair and its exact plain consequence."""

    resource: PatchResource
    link: PatchQuad
    form: PatchQuad
    plain: PatchQuad


@dataclass(frozen=True)
class LabelCandidate(MigrationCandidate):
    """Checked representation change with explicit source correspondence.

    Omitted assertions are inspectable source witnesses, including label metadata
    and relations that plain lexical labels cannot represent. No reverse identity
    reconstruction or whole-source equivalence follows from a verified report.
    """

    correspondence: tuple[LabelCorrespondence, ...] = ()
    omitted: tuple[PatchQuad, ...] = ()
