"""Explicit SKOS resource scope and ordinary selected rule settings."""

from dataclasses import dataclass

from rdflib import Graph
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
