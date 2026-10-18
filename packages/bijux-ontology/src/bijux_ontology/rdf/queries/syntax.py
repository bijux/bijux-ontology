"""Strict maintained SPARQL syntax qualification inside the owned query worker."""

from collections.abc import Mapping

import pyoxigraph
from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text


def check_query_syntax(
    text: str, namespaces: Mapping[str, URIRef], budget: Budget
) -> None:
    """Qualify scope and row-arity rules before interpreting a native answer.

    Call after the no-retrieval parse-tree guard, inside the deadline-owned
    worker. The empty in-memory store receives no source RDF, preserving exact
    source literals. Only syntax acceptance is used; its answer is discarded.
    """
    bound_text(text, budget)
    budget.consume("states", len(namespaces) + 1)
    budget.consume("bytes", 128 + 64 * len(namespaces))
    for name, iri in namespaces.items():
        bound_text(name, budget)
        bound_text(iri, budget)
    try:
        pyoxigraph.Store().query(
            text, prefixes={name: str(iri) for name, iri in namespaces.items()}
        )
    except SyntaxError as error:
        raise InputError(
            "SPARQL query violates syntax or variable-scope rules"
        ) from error
