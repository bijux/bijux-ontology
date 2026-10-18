"""Explicit graph partitions and finite original-term route selectors."""

from dataclasses import dataclass, field

from rdflib import BNode, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node


def _resource(value: object, budget: Budget) -> None:
    """Qualify an exact resource term before it participates in a route."""
    if not isinstance(value, URIRef | BNode):
        raise InputError("Graph routes require URIRef or owned BNode resources")
    bound_node(value, budget)
    if isinstance(value, URIRef):
        absolute_iri(value)


@dataclass(frozen=True)
class GraphRoute:
    """Move matching original quads; None names the explicit default graph.

    Subject and predicate filters are conjunctive; None leaves that dimension
    unrestricted. Blank selectors must be actual owned terms from the source.
    Graph inventory additions and removals are declared separately.
    """

    source: URIRef | BNode | None
    target: URIRef | BNode | None
    subjects: tuple[URIRef | BNode, ...] | None = field(default=None, kw_only=True)
    predicates: tuple[URIRef, ...] | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        """Own immutable typed filters without interpreting blank-node labels."""
        budget = Budget()
        for graph in (self.source, self.target):
            if graph is not None:
                _resource(graph, budget)
        for values, expected in (
            (self.subjects, URIRef | BNode),
            (self.predicates, URIRef),
        ):
            if values is None:
                continue
            if type(values) is not tuple or not values:
                raise InputError("Graph route filters require nonempty finite tuples")
            budget.consume("states", len(values))
            budget.consume("bytes", 64 + 16 * len(values))
            for value in values:
                budget.consume("work")
                if not isinstance(value, expected):
                    raise InputError("Graph route filter has an invalid RDF term")
                _resource(value, budget)
            if len(set(values)) != len(values):
                raise InputError("Graph route filters cannot repeat terms")
