"""Compare joint anonymous neighborhoods without guessing internal node matches."""

from collections import defaultdict, deque
from dataclasses import dataclass
from itertools import chain
from typing import cast

from rdflib import BNode, Graph, URIRef

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.facts.projection import FactProjection
from bijux_ontology.vocabulary.facts.types import FactGraph, SelectedFact
from bijux_ontology.vocabulary.sources.types import source_role

from .types import AnonymousChange, AnonymousDifferences, NamedAttachment


@dataclass
class _Group:
    """Union indistinguishable components under the complete attachment set."""

    facts: list[SelectedFact]
    qualified: bool = True


def _collect(
    view: VocabularyView, budget: Budget
) -> dict[frozenset[NamedAttachment], _Group]:
    """Retain only selected original contexts and side-local blank connectivity."""
    scopes = tuple(sorted({source_role(scope) for scope, _ in view._reader.scopes}))
    facts: list[SelectedFact] = []
    incident: dict[BNode, list[int]] = defaultdict(list)
    for scope, kind, name, graph in view._vocabulary._fact_sources(scopes, budget):
        bound_text(scope, budget)
        if name is not None:
            bound_node(name, budget)
        for subject, predicate, value in graph:
            budget.consume("work")
            blanks = {
                node for node in (subject, value, name) if isinstance(node, BNode)
            }
            if not blanks:
                continue
            budget.consume("triples")
            budget.consume("states", 1 + len(blanks))
            for node in (subject, predicate, value):
                bound_node(node, budget)
            number = len(facts)
            facts.append(
                SelectedFact(
                    (subject, cast(URIRef, predicate), value),
                    scope,
                    FactGraph(kind, name),
                    (),
                )
            )
            for blank in blanks:
                incident[blank].append(number)
    groups: dict[frozenset[NamedAttachment], _Group] = {}
    visited: set[BNode] = set()
    for seed in incident:
        budget.consume("work")
        if seed in visited:
            continue
        queue = deque(((seed, 0),))
        visited.add(seed)
        indexes: set[int] = set()
        while queue:
            blank, depth = queue.popleft()
            budget.consume("work")
            for index in incident[blank]:
                budget.consume("work")
                if index in indexes:
                    continue
                indexes.add(index)
                budget.consume("states")
                fact = facts[index]
                for adjacent in chain(fact.statement, (fact.graph.name,)):
                    if isinstance(adjacent, BNode) and adjacent not in visited:
                        if depth >= budget.limits.max_depth:
                            raise LimitExceeded(
                                "Anonymous neighborhood exceeds max_depth"
                            )
                        budget.consume("states")
                        visited.add(adjacent)
                        queue.append((adjacent, depth + 1))
        attachments: set[NamedAttachment] = set()
        qualified = True
        for index in indexes:
            budget.consume("work")
            fact = facts[index]
            subject, predicate, value = fact.statement
            if isinstance(fact.graph.name, BNode):
                qualified = False
            elif isinstance(subject, URIRef) and isinstance(value, BNode):
                budget.consume("states")
                attachments.add(
                    NamedAttachment(
                        str(subject),
                        str(predicate),
                        source_role(fact.source_scope),
                        fact.graph,
                    )
                )
        key = frozenset(attachments)
        budget.consume("states")
        group = groups.setdefault(key, _Group([]))
        group.qualified &= qualified and bool(attachments)
        group.facts.extend(facts[index] for index in sorted(indexes))
    return groups


def compare_anonymous(
    before: VocabularyView, after: VocabularyView, *, budget: Budget
) -> AnonymousDifferences:
    """Attribute changed joint components while preserving every owner and graph.

    Named fillers are references, not inferred owners. Components with the same
    complete attachment set are compared jointly by the maintained RDF oracle;
    neither component enumeration nor blank labels establish correspondence.
    """
    issues = IssueCollector(budget)
    changes: list[AnonymousChange] = []
    checked = 0
    complete = True
    coverage: list[tuple[SelectedFact, ...]] = [(), ()]
    try:
        old, new = _collect(before, budget), _collect(after, budget)
        for side, groups in enumerate((old, new)):
            count = sum(len(group.facts) for group in groups.values())
            budget.consume("states", count)
            budget.consume("bytes", 64 + count * 8)
            coverage[side] = tuple(
                chain.from_iterable(group.facts for group in groups.values())
            )
        keys = old.keys() | new.keys()
        budget.consume("work", len(keys) * max(1, len(keys).bit_length()))
        for key in sorted(keys, key=lambda values: repr(sorted(values, key=repr))):
            budget.consume("work")
            left, right = old.get(key, _Group([])), new.get(key, _Group([]))
            graphs = (Graph(bind_namespaces="none"), Graph(bind_namespaces="none"))
            for group, graph in zip((left, right), graphs, strict=True):
                projection = FactProjection(graph, budget)
                for fact in group.facts:
                    projection.add(fact)
            result = compare_rdf(*graphs, budget=budget)
            checked += 1
            if result.outcome == "disagreement":
                budget.consume("states")
                references: set[str] = set()
                for fact in chain(left.facts, right.facts):
                    for node in fact.statement:
                        budget.consume("work")
                        if isinstance(node, URIRef):
                            references.add(str(node))
                changes.append(
                    AnonymousChange(
                        tuple(sorted(key, key=repr)),
                        tuple(left.facts),
                        tuple(right.facts),
                        tuple(sorted(references)),
                        left.qualified and right.qualified,
                    )
                )
            elif not result.verified:
                issues.extend(result.issues)
                complete = False
    except LimitExceeded as error:
        issues.exhausted(str(error))
        complete = False
    return AnonymousDifferences(
        tuple(changes),
        Report(
            "vocabulary.anonymous_attachments",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            regime="asserted-anonymous-attachments",
        ),
        before=coverage[0],
        after=coverage[1],
    )
