"""Explicit and ordered-list-implied SKOS collection membership."""

from collections.abc import Mapping, Sequence

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.structure import read_rdf_list

from ..errors import InputError, LimitExceeded
from ..reports import Issue


def collection_memberships(
    graph: Graph,
    explicit: Mapping[Node, frozenset[Node]],
    *,
    required: Sequence[str] = (),
    budget: Budget,
    issues: IssueCollector,
) -> dict[Node, frozenset[Node]]:
    """Retain known members without treating missing ordered data as complete."""
    collections = (
        set(explicit)
        | set(graph.subjects(SKOS.memberList))
        | set(graph.subjects(RDF.type, SKOS.OrderedCollection))
        | {URIRef(iri) for iri in required}
    )
    cached: dict[Node, tuple[Node, ...] | None] = {}
    result: dict[Node, frozenset[Node]] = {}
    for collection in sorted(collections, key=lambda node: node.n3()):
        budget.consume("work")
        values = set(explicit.get(collection, ()))
        heads = tuple(graph.objects(collection, SKOS.memberList))
        ordered = (collection, RDF.type, SKOS.OrderedCollection) in graph
        if (ordered or heads) and len(heads) != 1:
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
                    cached[head] = None
                    issues.append(
                        Issue(
                            "skos.policy.member_list_malformed",
                            str(error),
                            str(collection),
                        )
                    )
            if cached[head] is not None:
                values.update(cached[head] or ())
        budget.consume("states", len(values))
        result[collection] = frozenset(values)
    return result
