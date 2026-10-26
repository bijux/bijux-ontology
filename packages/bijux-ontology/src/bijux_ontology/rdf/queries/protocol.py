"""Exact RDF terms in the private bounded local-query JSON protocol."""

from collections.abc import Mapping

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import literal_term, validate_node

QueryTerm = URIRef | BNode | Literal


def term_data(node: Node | None) -> dict[str, object] | None:
    """Preserve actual RDF kind, lexical form, datatype and language spelling."""
    if node is None:
        return None
    validate_node(node)
    if isinstance(node, Literal):
        return {
            "kind": "literal",
            "value": str(node),
            "datatype": str(node.datatype) if node.datatype else None,
            "language": node.language,
        }
    return {"kind": "blank" if isinstance(node, BNode) else "iri", "value": str(node)}


def reserve_solution_row(width: int, budget: Budget) -> None:
    """Pay one retained solution container and every cell, including unbound ones."""
    budget.consume("states", width + 1)
    budget.consume("bytes", 128 * (width + 1))


def read_term(
    value: object, blanks: dict[str, BNode], *, budget: Budget | None = None
) -> QueryTerm | None:
    """Bind blank labels only within this single request/result document."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("value"), str):
        raise InputError("Invalid RDF query term")
    kind, text = value.get("kind"), value["value"]
    if not isinstance(kind, str):
        raise InputError("Invalid RDF query term kind")
    if kind not in {"iri", "blank", "literal"}:
        raise InputError("Unsupported RDF query term kind")
    if kind in {"iri", "blank"} and set(value) != {"kind", "value"}:
        raise InputError("Invalid resource query term fields")
    if kind == "literal":
        if set(value) != {"kind", "value", "datatype", "language"}:
            raise InputError("Invalid literal query term fields")
        datatype, language = value.get("datatype"), value.get("language")
        if (datatype is not None and not isinstance(datatype, str)) or (
            language is not None and not isinstance(language, str)
        ):
            raise InputError("Invalid query literal metadata")
    if budget is not None:
        # Result wire fields are finite scalars. Fund constructors and their
        # original spellings before allocating an RDF term or blank inventory.
        budget.consume(
            "bytes",
            256 + 16 * sum(len(v) for v in value.values() if isinstance(v, str)),
        )
        for field in value.values():
            if isinstance(field, str):
                bound_text(field, budget)
    if kind == "iri":
        return URIRef(absolute_iri(text))
    if kind == "blank":
        if not text:
            raise InputError("Empty query blank identity")
        return blanks.setdefault(text, BNode())
    if kind == "literal":
        datatype, language = value.get("datatype"), value.get("language")
        return literal_term(text, datatype=datatype, language=language)
    raise InputError("Unsupported RDF query term kind")
