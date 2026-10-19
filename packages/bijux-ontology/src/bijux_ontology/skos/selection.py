"""Explicit SKOS resource scope and ordinary selected rule settings."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from rdflib import Graph, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.vocabulary.inspection import VocabularyView

from .structure import SkosStructure


@dataclass(frozen=True)
class SkosScope:
    """Owned supplied facts and the resources chosen for this check."""

    graph: Graph
    facts: SkosStructure
    resources: frozenset[Node]
    view: VocabularyView
    narrowed: bool
    explicit_concepts: bool

    def select(self, nodes: Iterable[Node]) -> frozenset[Node]:
        """Intersect supporting resources with the caller's publication scope."""
        selected = frozenset(nodes)
        return selected & self.resources if self.narrowed else selected


@dataclass(frozen=True)
class RuleSelection:
    """Transparent rule names and caller-owned publication requirements."""

    names: tuple[str, ...]
    languages: tuple[str, ...]
    language_fallback: tuple[str, ...]
    definition_predicates: tuple[str, ...]
    closed_targets: bool
    notation_collections: tuple[str, ...] = ()
    replacement_predicates: tuple[str, ...] = ()
    hierarchy_basis: Literal["asserted", "effective"] = "effective"


@dataclass(frozen=True)
class CollectionScope:
    """Collection obligations and supporting membership traversal stay distinct."""

    required: frozenset[Node]
    supporting: frozenset[Node]
    notation: frozenset[Node]


def collection_scope(scope: SkosScope, policy: RuleSelection) -> CollectionScope:
    """Plan collection roots once for validity, notation and active membership."""
    collections = frozenset(scope.graph.subjects(SKOS.memberList)) | frozenset(
        node for node, roles in scope.facts.roles.items() if SKOS.Collection in roles
    )
    required = (
        scope.select(collections)
        if set(policy.names) & {"collections", "deprecated-active"}
        else frozenset()
    )
    notation = (
        frozenset(URIRef(iri) for iri in policy.notation_collections)
        if set(policy.names) & {"notation-required", "notation-unique"}
        else frozenset()
    )
    required |= notation
    supporting = (
        collections - required if "deprecated-active" in policy.names else frozenset()
    )
    return CollectionScope(required, supporting, notation)
