"""Exact RDF terms in the private bounded local-query JSON protocol."""

from collections.abc import Mapping

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError
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


def read_term(value: object, blanks: dict[str, BNode]) -> QueryTerm | None:
    """Bind blank labels only within this single request/result document."""
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("value"), str):
        raise InputError("Invalid RDF query term")
    kind, text = value.get("kind"), value["value"]
    if not isinstance(kind, str):
        raise InputError("Invalid RDF query term kind")
    if kind in {"iri", "blank"} and set(value) != {"kind", "value"}:
        raise InputError("Invalid resource query term fields")
    if kind == "iri":
        return URIRef(absolute_iri(text))
    if kind == "blank":
        if not text:
            raise InputError("Empty query blank identity")
        return blanks.setdefault(text, BNode())
    if kind == "literal":
        if set(value) != {"kind", "value", "datatype", "language"}:
            raise InputError("Invalid literal query term fields")
        datatype, language = value.get("datatype"), value.get("language")
        if (datatype is not None and not isinstance(datatype, str)) or (
            language is not None and not isinstance(language, str)
        ):
            raise InputError("Invalid query literal metadata")
        return literal_term(text, datatype=datatype, language=language)
    raise InputError("Unsupported RDF query term kind")
