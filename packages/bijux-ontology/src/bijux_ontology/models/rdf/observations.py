"""Native term and asserted subject observations within one selected RDF graph."""

from dataclasses import dataclass

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.terms import bound_node


@dataclass(frozen=True)
class RdfObservation:
    """Native facts only; the caller retains the original term and graph identity."""

    kind: str
    datatype: str | None = None
    language: str | None = None
    identifier: str | None = None
    identifier_kind: str = "absent"
    types: tuple[str, ...] = ()

    @property
    def direction(self) -> None:
        """Ordinary term observation establishes no logical direction encoding."""
        return None


def observe_rdf_term(graph: Graph, node: Node, budget: Budget) -> RdfObservation:
    """Read exact native term traits without constructing JSON-LD or expansion."""
    bound_node(node, budget)
    if isinstance(node, Literal):
        return RdfObservation(
            "literal",
            datatype=str(
                node.datatype or (RDF.langString if node.language else XSD.string)
            ),
            language=node.language,
        )
    if isinstance(node, URIRef | BNode):
        kinds = []
        for kind in graph.objects(node, RDF.type):
            budget.consume("work")
            if isinstance(kind, URIRef):
                bound_node(kind, budget)
                kinds.append(str(kind))
        return RdfObservation(
            "iri" if isinstance(node, URIRef) else "blank",
            identifier=str(node) if isinstance(node, URIRef) else None,
            identifier_kind="iri" if isinstance(node, URIRef) else "blank",
            types=tuple(sorted(kinds)),
        )
    return RdfObservation("unknown")
