"""Shared maintained SPARQL parsing and local retrieval restrictions."""

from collections.abc import Iterable, Mapping
from typing import Any

import pyoxigraph
from rdflib import URIRef
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.queries.filters import (
    capture_lone_filter,
    require_filter_preservation,
)
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
    text: str,
    namespaces: Mapping[str, URIRef],
    budget: Budget,
    *,
    rdflib_execution: bool = False,
) -> Any:
    """Reject retrieval and qualify only the explicitly selected native boundary.

    Syntax admission remains independent of RDFLib's execution limitations for
    capable engines that evaluate the original supplied query text themselves.
    """
    budget.consume("work", len(text))
    try:
        parsed = parseQuery(text)
        if parsed[1].name not in {"AskQuery", "SelectQuery"}:
            raise InputError("Local queries support ASK and SELECT only")
        pending: list[tuple[object, int]] = [(parsed, 0)]
        lone_filters: list[CompValue] = []
        while pending:
            value, depth = pending.pop()
            budget.consume("work")
            budget.consume("states")
            if depth > budget.limits.max_depth:
                raise LimitExceeded("SPARQL query exceeds max_depth")
            if isinstance(value, CompValue):
                if rdflib_execution:
                    original = capture_lone_filter(value, budget)
                    if original is not None:
                        lone_filters.append(original)
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
        require_filter_preservation(lone_filters, budget)
        if unavailable is not None:
            raise unavailable
        return translated
    except (InputError, LimitExceeded):
        raise
    except RecursionError as error:
        raise LimitExceeded("SPARQL parser exceeded its nesting allowance") from error
    except Exception as error:
        raise InputError(f"Cannot parse local SPARQL query: {error}") from error
