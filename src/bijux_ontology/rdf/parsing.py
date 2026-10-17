"""Maintained RDF syntax parsing with bounded lexical-preserving conversion."""

from typing import Literal as TypingLiteral
from typing import cast

import pyoxigraph as ox
from rdflib import BNode, Dataset, URIRef

from bijux_ontology.limits import Budget

from ..errors import InputError, LimitExceeded, ResourceError
from .terms import oxigraph_term

RdfFormat = TypingLiteral["turtle", "nt", "xml", "trig", "nquads", "json-ld"]


RDF_FORMATS = {
    "turtle": ox.RdfFormat.TURTLE,
    "nt": ox.RdfFormat.N_TRIPLES,
    "xml": ox.RdfFormat.RDF_XML,
    "trig": ox.RdfFormat.TRIG,
    "nquads": ox.RdfFormat.N_QUADS,
}


def parse_dataset(
    payload: bytes,
    *,
    syntax: str,
    base: str | None,
    budget: Budget,
    max_triples: int,
    preserve_literals: bool,
) -> Dataset:
    """Parse bounded RDF syntax while retaining shared blank-node relations."""
    dataset = Dataset()
    if syntax == "xml":
        try:
            xml = payload.decode("utf-8")
        except UnicodeError as error:
            raise InputError("RDF/XML currently requires UTF-8 encoding") from error
        if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
            raise InputError("RDF/XML document types and entities are unsupported")
    try:
        parser = ox.parse(input=payload, format=RDF_FORMATS[syntax], base_iri=base)
        nodes: dict[str, BNode] = {}
        for count, quad in enumerate(parser, 1):
            budget.consume("triples")
            if count > max_triples:
                raise LimitExceeded("RDF document exceeds max_triples")
            target = (
                dataset.default_graph
                if isinstance(quad.graph_name, ox.DefaultGraph)
                else dataset.graph(
                    cast(
                        URIRef | BNode,
                        oxigraph_term(quad.graph_name, nodes, preserve_literals),
                    )
                )
            )
            target.add(
                (
                    oxigraph_term(quad.subject, nodes, preserve_literals),
                    oxigraph_term(quad.predicate, nodes, preserve_literals),
                    oxigraph_term(quad.object, nodes, preserve_literals),
                )
            )
        for prefix, namespace in parser.prefixes.items():
            dataset.bind(prefix, URIRef(namespace))
    except (SyntaxError, ValueError, OSError) as error:
        if isinstance(error, InputError):
            raise
        raise ResourceError(f"Cannot parse {syntax} RDF source: {error}") from error
    return dataset
