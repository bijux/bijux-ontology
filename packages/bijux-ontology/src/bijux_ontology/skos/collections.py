"""Explicit and ordered-list-implied SKOS collection membership."""

from collections.abc import Mapping

from rdflib import Graph
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.structure import read_rdf_list

from ..errors import InputError, LimitExceeded
from ..reports import Issue
from .selection import CollectionScope


def collection_memberships(
    graph: Graph,
    explicit: Mapping[Node, frozenset[Node]],
    *,
    scope: CollectionScope,
    budget: Budget,
    issues: IssueCollector,
) -> dict[Node, frozenset[Node]]:
    """Retain known members without treating missing ordered data as complete."""
    collections = scope.required | scope.supporting
    cached: dict[Node, tuple[Node, ...] | InputError] = {}
    result: dict[Node, frozenset[Node]] = {}
    for collection in sorted(collections, key=lambda node: node.n3()):
        budget.consume("work")
        values = set(explicit.get(collection, ()))
        heads = tuple(graph.objects(collection, SKOS.memberList))
        ordered = (collection, RDF.type, SKOS.OrderedCollection) in graph
        if collection in scope.required and (ordered or heads) and len(heads) != 1:
            issues.append(
                Issue(
                    "skos.policy.member_list_ambiguous",
                    "Complete ordered membership needs one supplied member list",
                    str(collection),
                    kind="unverified",
                )
            )
        for head in heads:
            budget.consume("work")
            if head not in cached:
                try:
                    members = read_rdf_list(
                        graph, head, max_items=budget.limits.max_work
                    )
                    budget.consume("work", len(members))
                    budget.consume("states", len(members))
                    cached[head] = members
                except LimitExceeded:
                    raise
                except InputError as error:
                    cached[head] = error
            cached_members = cached[head]
            if isinstance(cached_members, InputError):
                if collection in scope.required:
                    issues.append(
                        Issue(
                            "skos.policy.member_list_malformed",
                            str(cached_members),
                            str(collection),
                        )
                    )
            else:
                values.update(cached_members)
        budget.consume("states", len(values))
        result[collection] = frozenset(values)
    return result
