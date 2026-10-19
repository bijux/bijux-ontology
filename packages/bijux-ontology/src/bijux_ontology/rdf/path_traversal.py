"""Bounded finite RDF path traversal with exact RDF term identity."""

from typing import cast

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.terms import bound_node, literal_term, term_key


class PathIdentityUnavailable(InputError):
    """A literal intermediate lacks exact source identity for an inverse join."""


def finite_path_steps(
    path: RdfPath, budget: Budget, reverse: bool = False
) -> tuple[tuple[URIRef, bool], ...] | None:
    """Flatten the supported finite subset without a grammar or another AST."""
    budget.consume("work")
    if path.kind == "predicate":
        budget.consume("states")
        return ((cast(URIRef, path.node), reverse),)
    if path.kind == "inverse":
        return finite_path_steps(path.children[0], budget, not reverse)
    if path.kind != "sequence":
        return None
    values: list[tuple[URIRef, bool]] = []
    for child in reversed(path.children) if reverse else path.children:
        part = finite_path_steps(child, budget, reverse)
        if part is None:
            return None
        budget.consume("states", len(part))
        values.extend(part)
    return tuple(values)


def path_values(
    graph: Graph,
    subject: Node,
    steps: tuple[tuple[URIRef, bool], ...],
    budget: Budget,
    *,
    literal_identity: bool = True,
) -> tuple[Node, ...]:
    """Join scoped assertions, rejecting unqualified literal identity joins.

    A normalized literal can change the selected inverse edge even when final
    endpoints are IRIs. Callers must qualify original literal identity as well
    as endpoint fidelity. Resource-only joins do not need that qualification.
    """
    current = {subject}
    for predicate, inverse in steps:
        following: set[Node] = set()
        for node in current:
            budget.consume("work")
            if inverse and isinstance(node, Literal) and not literal_identity:
                raise PathIdentityUnavailable(
                    "Inverse path join requires exact intermediate literal identity"
                )
            lookups: tuple[Node, ...] = (node,)
            if (
                inverse
                and isinstance(node, Literal)
                and not node.language
                and node.datatype in (None, XSD.string)
            ):
                budget.consume("work", len(node) + 1)
                budget.consume("states", 2)
                budget.consume("bytes", 4 * len(node) + 64)
                alternate = literal_term(
                    str(node),
                    datatype=str(XSD.string) if node.datatype is None else None,
                )
                lookups = (node, alternate)
            for lookup in lookups:
                budget.consume("work")
                pattern = (
                    (None, predicate, lookup) if inverse else (lookup, predicate, None)
                )
                for triple in graph.triples(pattern):
                    budget.consume("work")
                    budget.consume("triples")
                    value = triple[0] if inverse else triple[2]
                    bound_node(value, budget)
                    if value not in following:
                        budget.consume("states")
                        following.add(value)
        current = following
    budget.consume("states", 2 * len(current))
    endpoints: dict[tuple[str | None, ...], Node] = {}
    for node in current:
        key = term_key(node)
        budget.consume("work", 1 + sum(len(part or "") for part in key))
        endpoints.setdefault(key, node)
    return tuple(endpoints.values())
