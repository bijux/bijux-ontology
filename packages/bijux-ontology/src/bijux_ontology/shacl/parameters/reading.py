"""Read original SHACL singleton terms and qualify their exact scalar forms."""

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import bound_node, literal_datatype


def count_value(value: Node, budget: Budget) -> int:
    """Admit exact nonnegative xsd:integer text under one explicit precision cap."""
    if not isinstance(value, Literal) or literal_datatype(value) != XSD.integer:
        raise InputError("SHACL counts require xsd:integer literals")
    # Both consumers already bound original RDF text to max_bytes before parsing.
    if len(value) > min(4096, budget.limits.max_bytes):
        raise LimitExceeded("SHACL count exceeds bounded integer representation")
    report = check_literal(value, budget=budget)
    if not report.verified:
        if any(issue.kind == "exhausted" for issue in report):
            raise LimitExceeded("SHACL count qualification exhausted its allowance")
        raise InputError("SHACL count has invalid integer syntax")
    budget.consume("work", len(value) + 1)
    result = int(str(value))
    if result < 0:
        raise InputError("SHACL count cannot be negative")
    return result


def boolean_value(value: Node) -> bool:
    """Interpret only the four exact xsd:boolean lexical forms, never truthiness."""
    if (
        not isinstance(value, Literal)
        or literal_datatype(value) != XSD.boolean
        or str(value) not in ("true", "false", "1", "0")
    ):
        raise InputError("SHACL boolean parameters require exact xsd:boolean text")
    return str(value) in ("true", "1")


class ParameterReader:
    """Reuse one supplied graph and budget without copying terms or building indexes."""

    def __init__(self, graph: Graph, budget: Budget) -> None:
        self.graph, self.budget = graph, budget

    def values(
        self, node: Node, predicate: URIRef, *, single: bool = False
    ) -> tuple[Node, ...]:
        """Retain original parameter terms and reject an ambiguous singleton early."""
        result = []
        for value in self.graph.objects(node, predicate):
            self.budget.consume("work")
            bound_node(value, self.budget)
            self.budget.consume("states")
            result.append(value)
            if single and len(result) > 1:
                raise InputError("A singleton SHACL parameter has multiple values")
        return tuple(result)

    def one(self, node: Node, predicate: URIRef) -> Node | None:
        """Keep absence distinct from an actual original singleton term."""
        values = self.values(node, predicate, single=True)
        return values[0] if values else None

    def count(self, node: Node, predicate: URIRef) -> int | None:
        """Read an optional qualified count, retaining source terms at the caller."""
        value = self.one(node, predicate)
        return None if value is None else count_value(value, self.budget)

    def boolean(self, node: Node, predicate: URIRef) -> bool:
        """An omitted boolean flag is false; a present flag must qualify exactly."""
        value = self.one(node, predicate)
        return False if value is None else boolean_value(value)
