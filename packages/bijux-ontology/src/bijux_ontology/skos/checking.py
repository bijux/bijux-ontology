"""SKOS integrity is separate from caller-selected publication requirements."""

from collections.abc import Sequence

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.vocabulary.inspection import VocabularyView, inspect_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary

from ..errors import InputError, LimitExceeded
from ..reports import Report
from .relations import check_relations
from .resources import _LABELS, check_resources
from .selection import RuleSelection, SkosScope
from .structure import structure
from .xl import XL as XL

_INTEGRITY = (
    "label-integrity",
    "resource-integrity",
    "relation-integrity",
    "mapping-integrity",
)
_RULES = frozenset(
    (
        *_INTEGRITY,
        "lexical-labels",
        "language-coverage",
        "collections",
        "acyclic",
        "scheme-required",
        "single-scheme",
        "same-scheme-hierarchy",
        "isolated",
        "mapping-targets",
        "notation-required",
        "notation-unique",
        "deprecated-active",
        "definitions",
        "xl",
    )
)


def check_skos(
    vocabulary: Vocabulary | Graph | VocabularyView,
    *,
    scopes: Sequence[str] = ("local", "imports"),
    concepts: Sequence[str] = (),
    schemes: Sequence[str] = (),
    rules: Sequence[str] = _INTEGRITY,
    languages: Sequence[str] = (),
    language_fallback: Sequence[str] = (),
    definition_predicates: Sequence[str] = (str(SKOS.definition),),
    closed_targets: bool = False,
    notation_collections: Sequence[str] = (),
    replacement_predicates: Sequence[str] = (),
    limits: Limits | None = None,
) -> Report:
    """Check advertised finite SKOS implications and explicit authoring policies.

    Default rules cover selected core integrity conditions only. Cycles, missing
    language coverage, schemes, definitions, notations and unresolved targets are
    opt-in policies; they are never relabelled universal SKOS contradictions.
    ``concepts`` selects exact resources; ``schemes`` narrows known membership.
    Notation collections intersect that selection. Supporting ordered lists do
    not select their other members for publication checks.
    """
    if isinstance(rules, (str, bytes)) or not rules or set(rules) - _RULES:
        raise InputError("Select supported nonempty SKOS rule names")
    if type(closed_targets) is not bool:
        raise InputError("closed_targets must be a boolean")
    for iri in (
        *concepts,
        *schemes,
        *definition_predicates,
        *notation_collections,
        *replacement_predicates,
    ):
        absolute_iri(iri)
    for tag in (*languages, *language_fallback):
        language_tag(tag)
    selected_limits = (
        limits
        if limits is not None
        else vocabulary.limits
        if isinstance(vocabulary, (Vocabulary, VocabularyView))
        else None
    )
    budget = Budget(selected_limits)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        view = (
            vocabulary
            if isinstance(vocabulary, VocabularyView)
            else inspect_vocabulary(
                vocabulary, scopes=scopes, limits=limits, budget=budget
            )
        )
        graph = view._read()
        facts = structure(graph, budget)
        resources: set[Node] = (
            {URIRef(iri) for iri in concepts}
            if concepts
            else {node for node, roles in facts.roles.items() if SKOS.Concept in roles}
            | {node for predicate in _LABELS for node in graph.subjects(predicate)}
        )
        if "xl" in rules and not concepts:
            resources.update(
                node
                for predicate in (XL.prefLabel, XL.altLabel, XL.hiddenLabel)
                for node in graph.subjects(predicate)
            )
            resources.update(graph.subjects(RDF.type, XL.Label))
        if schemes:
            selected_schemes = {URIRef(iri) for iri in schemes}
            resources = {
                node
                for node in resources
                if facts.schemes.get(node, frozenset()) & selected_schemes
            }
        if not resources and not concepts:
            resources = (
                set(facts.roles)
                if not schemes
                else {
                    node
                    for node, memberships in facts.schemes.items()
                    if memberships & {URIRef(iri) for iri in schemes}
                }
            )
        selected_scope = SkosScope(
            graph,
            facts,
            frozenset(resources),
            view,
            bool(concepts or schemes),
            bool(concepts),
        )
        policy = RuleSelection(
            tuple(rules),
            tuple(languages),
            tuple(language_fallback),
            tuple(definition_predicates),
            closed_targets,
            tuple(notation_collections),
            tuple(replacement_predicates),
        )
        checked += check_resources(selected_scope, policy, budget, issues)
        checked += check_relations(selected_scope, policy, budget, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="skos")
        complete = False
    return Report(
        "skos",
        checked,
        tuple(issues),
        requested=checked if complete else checked + 1,
        complete=complete,
        regime="skos-selected-closure",
        families=tuple(rules),
    )
