"""Parsed SHACL query dependencies under the maintained prefix environment."""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.reports import Issue

if TYPE_CHECKING:
    from pyshacl.shape import Shape

_CASTS = frozenset(
    (
        XSD.boolean,
        XSD.integer,
        XSD.decimal,
        XSD.double,
        XSD.float,
        XSD.dateTime,
        XSD.string,
    )
)


def query_functions(
    shape: "Shape", node: Node, text: Node, budget: Budget
) -> set[URIRef]:
    """Resolve actual function-call IRIs, excluding property names and literals."""
    from pyshacl.helper.sparql_query_helper import SPARQLQueryHelper

    if not isinstance(text, Literal):
        raise InputError("SHACL query text must be a literal", location=str(node))
    helper = SPARQLQueryHelper(shape, node, str(text))  # type: ignore[no-untyped-call]
    helper.collect_prefixes()  # type: ignore[no-untyped-call]
    try:
        parsed = parseQuery(helper.apply_prefixes(str(text)))  # type: ignore[no-untyped-call]
        algebra = translateQuery(
            parsed, initNs=dict(shape.sg.graph.namespaces())
        ).algebra
    except Exception as error:
        # RDFLib uses a generic exception for unresolved query prefixes.
        raise InputError(
            "SHACL query context could not be resolved", location=str(node)
        ) from error
    result = set()
    pending: list[object] = [algebra]
    while pending:
        budget.consume("work")
        value = pending.pop()
        if isinstance(value, CompValue):
            if value.name in ("ServiceGraphPattern", "Service", "DatasetClause"):
                raise InputError(
                    "SHACL queries cannot use SERVICE or dataset retrieval",
                    location=str(node),
                )
            if value.name == "Function":
                iri = value.get("iri")
                if not isinstance(iri, URIRef):
                    raise InputError(
                        "Unresolved SHACL function IRI", location=str(node)
                    )
                result.add(iri)
            pending.extend(value.values())
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            pending.extend(value)
    return result


def check_function_dependencies(
    shape: "Shape", nodes: Iterable[Node], budget: Budget, issues: IssueCollector
) -> None:
    """Qualify selected calls and supplied transitive bodies without enabling them."""
    graph = shape.sg.graph
    pending = list(nodes)
    seen: set[Node] = set()
    unavailable: set[URIRef] = set()
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        budget.consume("states")
        for predicate in (SH.select, SH.ask):
            for text in graph.objects(node, predicate):
                for function in query_functions(shape, node, text, budget):
                    declared = (function, RDF.type, SH.SPARQLFunction) in graph
                    if function in _CASTS and not declared:
                        continue
                    if function not in unavailable:
                        unavailable.add(function)
                        issues.append(
                            Issue(
                                "shacl.function_unavailable",
                                "Called function has no enabled implementation",
                                str(function),
                                kind="unverified",
                                actual=str(node),
                            )
                        )
                    if declared:
                        bodies = tuple(graph.objects(function, SH.select)) + tuple(
                            graph.objects(function, SH.ask)
                        )
                        if len(bodies) != 1:
                            issues.append(
                                Issue(
                                    "shacl.function_malformed",
                                    "SPARQL function needs one query body",
                                    str(function),
                                    kind="unverified",
                                )
                            )
                        pending.append(function)
