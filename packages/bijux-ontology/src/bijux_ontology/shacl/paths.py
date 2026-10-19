"""Finite SHACL property paths with explicit malformed and bounded outcomes."""

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.paths import RdfPath as ShaclPath
from bijux_ontology.rdf.structure import read_rdf_list

__all__ = ["ShaclPath", "decode_path"]

_OPERATORS = (
    (SH.inversePath, "inverse"),
    (SH.alternativePath, "alternative"),
    (SH.zeroOrMorePath, "zero_or_more"),
    (SH.oneOrMorePath, "one_or_more"),
    (SH.zeroOrOnePath, "zero_or_one"),
)


def decode_path(
    graph: Graph,
    node: Node,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShaclPath:
    """Decode a property path without following cyclic or unbounded RDF structure."""
    budget = budget if budget is not None else Budget(limits)

    def walk(current: Node, active: frozenset[Node], depth: int) -> ShaclPath:
        """Decode one finite operator while retaining active cycle guards."""
        budget.consume("work")
        budget.consume("states")
        if depth > min(budget.limits.max_depth, 128):
            raise LimitExceeded("Property path exceeds bounded decoder depth")
        if isinstance(current, URIRef):
            return ShaclPath("predicate", current)
        if not isinstance(current, BNode) or current in active:
            raise InputError("Property path must be finite and use IRI or blank nodes")
        operators = tuple(
            (kind, value)
            for predicate, kind in _OPERATORS
            for value in graph.objects(current, predicate)
        )
        sequence = (current, RDF.first, None) in graph or (
            current,
            RDF.rest,
            None,
        ) in graph
        if len(operators) + int(sequence) != 1:
            raise InputError("Property path requires exactly one structural operator")
        kind, value = ("sequence", current) if sequence else operators[0]
        if kind in ("sequence", "alternative"):
            members = read_rdf_list(graph, value, max_items=budget.limits.max_work)
            budget.consume("work", len(members))
            if len(members) < 2:
                raise InputError("Sequence and alternative paths require two members")
        else:
            members = (value,)
        children = tuple(
            walk(member, active | {current}, depth + 1) for member in members
        )
        return ShaclPath(kind, current, children)

    try:
        return walk(node, frozenset(), 0)
    except (InputError, LimitExceeded) as error:
        return ShaclPath("unsupported", node, limitation=str(error))
