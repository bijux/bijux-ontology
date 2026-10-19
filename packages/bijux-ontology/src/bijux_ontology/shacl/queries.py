"""Parsed SHACL query dependencies under the maintained prefix environment."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rdflib import Literal, URIRef, Variable
from rdflib.namespace import RDF, SH, XSD
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.reports import Issue

if TYPE_CHECKING:
    from pyshacl.functions.shacl_function import SPARQLFunction
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


@dataclass(frozen=True)
class FunctionCall:
    """One parsed RDF function IRI and its supplied argument count."""

    iri: URIRef
    arguments: int


@dataclass(frozen=True)
class QueryAnalysis:
    """Parsed query form, explicit result variables and actual extension calls."""

    form: str
    projection: tuple[Variable, ...] | None
    calls: tuple[FunctionCall, ...]
    text: str


def _prefix(value: CompValue) -> str:
    # The second CompValue.get argument controls evaluation, not a missing default.
    if "prefix" in value:
        return str(value["prefix"])
    return ""


def _nodes(value: object, budget: Budget) -> Iterator[CompValue]:
    pending = [value]
    while pending:
        budget.consume("work")
        current = pending.pop()
        if isinstance(current, CompValue):
            yield current
            pending.extend(current.values())
        elif isinstance(current, Iterable) and not isinstance(current, (str, bytes)):
            pending.extend(current)


def analyze_query(
    shape: "Shape | SPARQLFunction", node: Node, text: Node, budget: Budget
) -> QueryAnalysis:
    """Resolve actual calls under maintained SHACL query-prefix handling."""
    from pyshacl.helper.sparql_query_helper import SPARQLQueryHelper

    if not isinstance(text, Literal):
        raise InputError("SHACL query text must be a literal", location=str(node))
    helper = SPARQLQueryHelper(shape, node, str(text))  # type: ignore[no-untyped-call]
    helper.collect_prefixes()  # type: ignore[no-untyped-call]
    try:
        expanded = helper.apply_prefixes(str(text))  # type: ignore[no-untyped-call]
        budget.consume("bytes", len(expanded.encode()))
        budget.consume("work", len(expanded))
        parsed = parseQuery(expanded)
        referenced_prefixes = {
            _prefix(part) for part in _nodes(parsed, budget) if part.name == "pname"
        }
        algebra = translateQuery(
            parsed, initNs=dict(shape.sg.graph.namespaces())
        ).algebra
    except LimitExceeded:
        raise
    except Exception as error:
        # RDFLib uses a generic exception for unresolved query prefixes.
        raise InputError(
            "SHACL query context could not be resolved", location=str(node)
        ) from error
    result: list[FunctionCall] = []
    for value in _nodes(algebra, budget):
        if value.name in ("ServiceGraphPattern", "Service", "DatasetClause"):
            raise InputError(
                "SHACL queries cannot use SERVICE or dataset retrieval",
                location=str(node),
            )
        if value.name == "Function":
            iri = value.get("iri")
            if not isinstance(iri, URIRef):
                raise InputError("Unresolved SHACL function IRI", location=str(node))
            arguments = ()
            if "expr" in value:
                arguments = value["expr"]
            result.append(FunctionCall(iri, len(arguments)))
    projected = tuple(algebra["PV"]) if "projection" in parsed[1] else None
    declared = {_prefix(part) for part in parsed[0] if part.name == "PrefixDecl"}
    prefixes = "\n".join(
        f"PREFIX {prefix or ''}: {namespace.n3()}"
        for prefix, namespace in shape.sg.graph.namespaces()
        if prefix in referenced_prefixes and prefix not in declared
    )
    return QueryAnalysis(
        parsed[1].name, projected, tuple(result), prefixes + "\n" + expanded
    )


def query_functions(
    shape: "Shape", node: Node, text: Node, budget: Budget
) -> set[URIRef]:
    """Return called IRIs without interpreting their runtime SPARQL error behavior."""
    return {call.iri for call in analyze_query(shape, node, text, budget).calls}


def check_function_dependencies(
    shape: "Shape",
    nodes: Iterable[Node],
    budget: Budget,
    issues: IssueCollector,
    enabled: frozenset[URIRef] = frozenset(),
    normalized: list[tuple[Node, URIRef, Literal]] | None = None,
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
                analysis = analyze_query(shape, node, text, budget)
                if normalized is not None:
                    normalized.append((node, predicate, Literal(analysis.text)))
                for function in {call.iri for call in analysis.calls}:
                    if function in enabled:
                        continue
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
