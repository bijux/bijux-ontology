"""Shared maintained SPARQL parsing and local retrieval restrictions."""

from collections.abc import Iterable, Mapping
from typing import Any

import pyoxigraph
from rdflib import URIRef
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.queries.literals import check_literal_scope


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
    except RuntimeError as error:
        raise ResourceError(
            "Native SPARQL syntax qualification cannot support this query"
        ) from error


def prepare_local_query(
    text: str, namespaces: Mapping[str, URIRef], budget: Budget
) -> Any:
    """Reject retrieval in every maintained parse-tree branch before evaluation."""
    budget.consume("work", len(text))
    try:
        parsed = parseQuery(text)
        if parsed[1].name not in {"AskQuery", "SelectQuery"}:
            raise InputError("Local queries support ASK and SELECT only")
        pending: list[tuple[object, int]] = [(parsed, 0)]
        while pending:
            value, depth = pending.pop()
            budget.consume("work")
            budget.consume("states")
            if depth > budget.limits.max_depth:
                raise LimitExceeded("SPARQL query exceeds max_depth")
            if isinstance(value, CompValue):
                if value.name in {"ServiceGraphPattern", "DatasetClause"}:
                    raise InputError(
                        "Local queries disallow SERVICE and FROM/FROM NAMED"
                    )
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                pending.extend((child, depth + 1) for child in value)
        unavailable = None
        try:
            check_query_syntax(text, namespaces, budget)
        except ResourceError as error:
            # A parsed query may use a native unsupported function. Retain the
            # more precise literal-construction boundary when it also applies.
            unavailable = error
        translated = translateQuery(parsed, initNs=namespaces)
        check_literal_scope(translated.algebra, budget)
        if unavailable is not None:
            raise unavailable
        return translated
    except (InputError, LimitExceeded):
        raise
    except RecursionError as error:
        raise LimitExceeded("SPARQL parser exceeded its nesting allowance") from error
    except Exception as error:
        raise InputError(f"Cannot parse local SPARQL query: {error}") from error
