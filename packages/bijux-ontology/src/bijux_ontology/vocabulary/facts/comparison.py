"""Bounded exact selected facts with maintained anonymous correspondence."""

from dataclasses import dataclass
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.roles.types import Statement
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.types import source_role
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .projection import FactProjection
from .types import (
    AnonymousFactComparison,
    FactGraph,
    FactPredicate,
    FactSelection,
    SelectedFact,
    SelectedFactDifference,
    SelectedFactDifferences,
)

_Key = tuple[str, str, tuple[str | None, ...], tuple[tuple[str | None, ...], ...]]
_Source = Vocabulary | Graph | PreparedSource | VocabularyView


@dataclass
class _Inventory:
    """One selected side's exact ground facts and joint anonymous projection."""

    facts: dict[_Key, SelectedFact]
    anonymous: Graph
    anonymous_facts: list[SelectedFact]
    scopes: set[str]
    checked: int = 0


def _metadata(selection: FactSelection, budget: Budget) -> dict[str, FactPredicate]:
    """Qualify finite selector text before hashing or scanning owned contexts."""
    rules: dict[str, FactPredicate] = {}
    for rule in selection.predicates:
        budget.consume("work")
        budget.consume("states")
        bound_text(rule.iri, budget)
        for role in (rule.subject_role, rule.object_role):
            if role is not None:
                bound_text(role, budget)
        rules[rule.iri] = rule
    for values in (selection.entities, selection.positions, selection.scopes):
        budget.consume("states", len(values))
        for value in values:
            bound_text(value, budget)
    for graph in selection.graphs or ():
        budget.consume("work")
        budget.consume("states")
        if graph.name is not None:
            bound_node(graph.name, budget)
    return rules


def _source(source: _Source, selection: FactSelection, budget: Budget) -> Vocabulary:
    """Use original owned provenance without widening an inspection view."""
    if isinstance(source, VocabularyView):
        budget.consume("work", len(source._reader.scopes))
        available = {source_role(scope) for scope, _ in source._reader.scopes}
        wanted = set(selection.scopes)
        for scope, _ in source._vocabulary._sources:
            budget.consume("work")
            role = source_role(scope)
            if role in wanted and role not in available:
                raise InputError("Fact selection widens the supplied vocabulary view")
        return source._vocabulary
    return (
        source
        if isinstance(source, Vocabulary)
        else load_vocabulary(source, budget=budget)
    )


def _graph_selected(graph: FactGraph, selection: FactSelection, budget: Budget) -> bool:
    """Blank selectors require an actual owned identity, never a label match."""
    if selection.graphs is None:
        return True
    for chosen in selection.graphs:
        budget.consume("work", 1 + min(len(graph.name or ""), len(chosen.name or "")))
        if graph.kind == chosen.kind and (
            graph.name is chosen.name
            if isinstance(graph.name, BNode) or isinstance(chosen.name, BNode)
            else graph.name == chosen.name
        ):
            return True
    return False


def _entity_selected(
    statement: Statement,
    graph: URIRef | BNode | None,
    entities: frozenset[str],
    positions: frozenset[str],
    budget: Budget,
) -> bool:
    """Filter original RDF positions before interpreting selected axiom roles."""
    if not entities:
        return True
    value = statement[2]
    terms = (
        ("subject", statement[0]),
        ("predicate", statement[1]),
        ("object", value),
        ("datatype", literal_datatype(value) if isinstance(value, Literal) else None),
        ("graph", graph),
    )
    for position, term in terms:
        budget.consume("work")
        if position in positions and isinstance(term, URIRef):
            bound_node(term, budget)
            if str(term) in entities:
                return True
    return False


def _collect(
    source: _Source,
    selection: FactSelection,
    rules: dict[str, FactPredicate],
    budget: Budget,
    issues: IssueCollector,
) -> _Inventory:
    """Complete one selected inventory before any absence can become a change."""
    inventory = _Inventory({}, Graph(bind_namespaces="none"), [], set())
    projection = FactProjection(inventory.anonymous, budget)
    seen: set[_Key] = set()
    blank_graphs: dict[int, BNode] = {}
    entities = frozenset(selection.entities)
    positions = frozenset(selection.positions)
    vocabulary = _source(source, selection, budget)
    for scope, kind, name, graph in vocabulary._fact_sources(selection.scopes, budget):
        budget.consume("work")
        bound_text(scope, budget)
        if name is not None:
            bound_node(name, budget)
        if isinstance(name, BNode):
            budget.consume("states")
            blank_graphs[id(name)] = name
        owned_graph = FactGraph(kind, name)
        if not _graph_selected(owned_graph, selection, budget):
            continue
        for subject, predicate, value in graph:
            budget.consume("work", len(cast(URIRef, predicate)) + 1)
            rule = rules.get(str(predicate))
            if rule is None:
                continue
            statement = (subject, predicate, value)
            if not _entity_selected(statement, name, entities, positions, budget):
                continue
            occurrences = classify_statement(
                statement,
                source_scope=scope,
                graph=name,
                subject_role=rule.subject_role,
                object_role=rule.object_role,
                budget=budget,
            )
            fact = SelectedFact(statement, scope, owned_graph, occurrences)
            key: _Key = (
                source_role(scope),
                kind,
                term_key(name),
                tuple(term_key(node) for node in statement),
            )
            budget.consume("work")
            if key in seen:
                continue
            budget.consume("states")
            seen.add(key)
            budget.consume("triples")
            budget.consume("states")
            budget.consume("bytes", 256)
            inventory.checked += 1
            if any(
                occurrence.role == "unknown"
                and (
                    isinstance(occurrence.term, URIRef | BNode)
                    or occurrence.position == "subject"
                    and rule.subject_role not in (None, "unknown")
                    or occurrence.position == "object"
                    and rule.object_role not in (None, "unknown")
                )
                for occurrence in occurrences
                if occurrence.position in ("subject", "object")
            ):
                issues.append(
                    Issue(
                        "vocabulary.fact_role_unverified",
                        "A selected fact has an explicitly unknown endpoint role",
                        str(subject),
                        kind="unverified",
                        actual=str(predicate),
                    )
                )
            if any(isinstance(node, BNode) for node in (*statement, name)):
                projection.add(fact)
                budget.consume("states")
                inventory.anonymous_facts.append(fact)
                inventory.scopes.add(source_role(scope))
                continue
            inventory.facts.setdefault(key, fact)
    for chosen in selection.graphs or ():
        budget.consume("work")
        if isinstance(chosen.name, BNode) and id(chosen.name) not in blank_graphs:
            issues.append(
                Issue(
                    "vocabulary.fact_graph_unverified",
                    "Blank graph selection lacks an owned identity in this snapshot",
                    "vocabulary.facts",
                    kind="unverified",
                )
            )
    return inventory


def compare_selected_facts(
    before: _Source,
    after: _Source,
    selection: FactSelection,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SelectedFactDifferences:
    """Compare exact selected assertions without inferring their business meaning.

    Both inventories must complete before named additions/removals are emitted.
    Anonymous assertions use one joint, source/graph-qualified maintained RDF
    comparison; a changed structure does not establish a unique affected owner.
    Unknown logical roles remain visible independently of exact RDF identity.
    """
    if not isinstance(selection, FactSelection):
        raise InputError("Expected a typed FactSelection")
    if not isinstance(
        before, Vocabulary | Graph | PreparedSource | VocabularyView
    ) or not isinstance(after, Vocabulary | Graph | PreparedSource | VocabularyView):
        raise InputError("Expected owned vocabulary or explicit RDF sources")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    differences: list[SelectedFactDifference] = []
    anonymous: list[AnonymousFactComparison] = []
    checked = 0
    complete = True
    try:
        rules = _metadata(selection, operation)
        left = _collect(before, selection, rules, operation, issues)
        right = _collect(after, selection, rules, operation, issues)
        checked = left.checked + right.checked
        operation.consume("work", len(left.facts) + len(right.facts))
        operation.consume("states", len(left.facts) + len(right.facts))
        operation.consume("bytes", 128 * (len(left.facts) + len(right.facts)))
        keys = left.facts.keys() | right.facts.keys()
        text_size = sum(
            len(key[0])
            + len(key[1])
            + sum(len(part or "") for node in (key[2], *key[3]) for part in node)
            for key in keys
        )
        operation.consume(
            "work", (len(keys) + text_size) * max(1, len(keys).bit_length())
        )
        operation.consume("bytes", 256 * len(keys) + 6 * text_size)
        for key in sorted(keys, key=repr):
            operation.consume("work")
            old, new = left.facts.get(key), right.facts.get(key)
            if (old is None) == (new is None):
                continue
            fact = cast(SelectedFact, new if old is None else old)
            operation.consume("states")
            issues.append(
                Issue(
                    "vocabulary.fact_changed",
                    "Selected supplied RDF facts differ",
                    str(fact.statement[0]),
                    severity="info",
                    actual="added" if old is None else "removed",
                )
            )
            differences.append(
                SelectedFactDifference("added" if old is None else "removed", fact)
            )
        if left.anonymous or right.anonymous:
            count = len(left.anonymous_facts) + len(right.anonymous_facts)
            operation.consume("states", count + 1)
            operation.consume("bytes", 128 + 8 * count)
            old_anonymous = tuple(left.anonymous_facts)
            new_anonymous = tuple(right.anonymous_facts)
            raw = compare_rdf(left.anonymous, right.anonymous, budget=operation)
            anonymous.append(
                AnonymousFactComparison(
                    tuple(sorted(left.scopes | right.scopes)),
                    old_anonymous,
                    new_anonymous,
                    raw,
                )
            )
            checked += raw.checked
            if not raw.complete:
                issues.extend(raw.issues)
                complete = False
            elif any(issue.code == "rdf.structure_changed" for issue in raw.issues):
                issues.append(
                    Issue(
                        "vocabulary.fact_attribution_unverified",
                        "Selected anonymous structure changed; "
                        "affected ownership is unverified",
                        "vocabulary.facts",
                        kind="unverified",
                    )
                )
        if not checked:
            checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error), location="vocabulary.facts")
        complete = False
    return SelectedFactDifferences(
        tuple(differences),
        tuple(anonymous),
        Report(
            "vocabulary.facts",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            regime="asserted-scoped-facts",
            families=("vocabulary.facts",),
        ),
    )
