"""Finite maintained syntax qualification and exact selected graph preparation."""

from collections.abc import Iterable, Iterator
from typing import Any

from rdflib import BNode, Graph, Literal, URIRef, Variable
from rdflib.plugins.sparql.algebra import translateQuery, translateUpdate
from rdflib.plugins.sparql.parser import Var, parseQuery, parseUpdate
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.queries.literals import check_literal_scope
from bijux_ontology.rdf.records.types import (
    PatchBlank,
    PatchLiteral,
    PatchValue,
    SnapshotWitness,
)
from bijux_ontology.rdf.terms import bound_node, literal_term


class TransformUnavailable(InputError):
    """The actual program or selected source exceeds the explicit finite profile."""


def syntax_nodes(value: object, budget: Budget) -> Iterator[object]:
    """Bound every maintained parse-tree edge before retaining traversal state."""
    budget.consume("states")
    pending = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        budget.consume("work")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("SPARQL transformation syntax exceeds max_depth")
        yield current
        children = current.values() if isinstance(current, CompValue) else current
        if isinstance(children, Iterable) and not isinstance(children, str | bytes):
            for child in children:
                budget.consume("work")
                budget.consume("states")
                pending.append((child, depth + 1))


def _field(value: CompValue, name: str, default: Any) -> Any:
    """CompValue.get takes evaluation context rather than a missing-key default."""
    if name in value:
        return value[name]
    return default


def checked_program(text: str, kind: str, budget: Budget) -> Any:
    """Reject prohibited operations on maintained syntax before native evaluation."""
    bound_text(text, budget)
    try:
        parsed: Any = parseQuery(text) if kind == "CONSTRUCT" else parseUpdate(text)
        if kind == "CONSTRUCT":
            if parsed[1].name != "ConstructQuery":
                raise InputError("A CONSTRUCT program requires that actual query form")
        else:
            requests = _field(parsed, "request", ())
            if len(requests) != 1 or requests[0].name not in (
                "InsertData",
                "DeleteData",
                "DeleteWhere",
                "Modify",
            ):
                raise TransformUnavailable(
                    "Local UPDATE requires one supported operation"
                )
            operation = requests[0]
            if operation.name in ("InsertData", "DeleteData") and any(
                isinstance(node, Variable) for node in syntax_nodes(operation, budget)
            ):
                raise InputError("SPARQL DATA operations cannot contain variables")
            deleted = (
                operation
                if operation.name in ("DeleteData", "DeleteWhere")
                else _field(operation, "delete", None)
            )
            if deleted is not None and any(
                isinstance(node, BNode) for node in syntax_nodes(deleted, budget)
            ):
                raise TransformUnavailable(
                    "Delete templates cannot contain blank nodes"
                )
        container = parsed[1] if kind == "CONSTRUCT" else operation
        where = _field(container, "where", None)
        if where is not None and any(
            isinstance(node, BNode) for node in syntax_nodes(where, budget)
        ):
            raise TransformUnavailable(
                "WHERE blank syntax has unqualified basic-pattern scope"
            )
        prefixes = set()
        referenced = set()
        for value in syntax_nodes(parsed, budget):
            if isinstance(value, URIRef):
                bound_node(value, budget)
                absolute_iri(value)
            elif isinstance(value, CompValue):
                if value.name in (
                    "Bind",
                    "SubSelect",
                    "GroupClause",
                    "HavingClause",
                    "InlineData",
                    "ValuesClause",
                ) or value.name.startswith("Aggregate_"):
                    raise TransformUnavailable(
                        "Assignment, VALUES, subqueries and aggregation are unsupported"
                    )
                if (
                    value.name
                    in (
                        "ServiceGraphPattern",
                        "Service",
                        "DatasetClause",
                        "GraphGraphPattern",
                        "QuadsNotTriples",
                        "UsingClause",
                        "Load",
                        "Clear",
                        "Drop",
                        "Create",
                        "Add",
                        "Move",
                        "Copy",
                    )
                    or "withClause" in value
                    or "using" in value
                ):
                    raise TransformUnavailable(
                        "External retrieval and graph operations are prohibited"
                    )
                if value.name == "Function":
                    raise TransformUnavailable("Custom IRI functions are unsupported")
                if value.name == "PrefixDecl":
                    prefix = _field(value, "prefix", "")
                    bound_text(prefix, budget)
                    prefixes.add(prefix)
                if value.name == "pname":
                    prefix = _field(value, "prefix", "")
                    bound_text(prefix, budget)
                    referenced.add(prefix)
        if not referenced <= prefixes:
            raise TransformUnavailable("Program prefixes must be explicitly declared")
        compiled = (
            translateQuery(parsed, initNs={})
            if kind == "CONSTRUCT"
            else translateUpdate(parsed, initNs={})
        )
        check_literal_scope(compiled.algebra, budget)
        for value in syntax_nodes(compiled.algebra, budget):
            if (
                isinstance(value, Literal)
                and value.datatype is None
                and value.language is None
            ):
                raise TransformUnavailable(
                    "RDFLib plain query constants require explicit xsd:string"
                )
        return compiled
    except (InputError, LimitExceeded):
        raise
    except RecursionError as error:
        raise LimitExceeded(
            "Maintained SPARQL parser exceeded its nesting bound"
        ) from error
    except Exception as error:
        raise InputError("Cannot parse the selected SPARQL transformation") from error


def variable_name(value: object, budget: Budget) -> str:
    """Validate parameter names with the maintained SPARQL variable grammar."""
    if type(value) is not str or not value:
        raise InputError("SPARQL parameter names require nonempty plain text")
    bound_text(value, budget)
    if len(value) + 1 > budget.limits.max_bytes:
        raise LimitExceeded("SPARQL parameter name exceeds max_bytes")
    budget.consume("bytes", len(value) + 1)
    budget.consume("work", len(value) + 1)
    try:
        parsed = Var.parse_string("?" + value, parse_all=True)
    except Exception as error:
        raise InputError("Invalid SPARQL parameter variable name") from error
    if len(parsed) != 1 or str(parsed[0]) != value:
        raise InputError("Invalid SPARQL parameter variable name")
    return value


def graph_from_witness(
    witness: SnapshotWitness, budget: Budget
) -> tuple[Graph, dict[str, BNode]]:
    """Create one private graph, retaining the exact input blank-token association."""
    if witness.kind == "dataset":
        raise TransformUnavailable("Transformations require one physical graph")
    graph = Graph(bind_namespaces="none")
    blanks: dict[str, BNode] = {}

    def node(value: PatchValue) -> Node:
        """Reconstruct exact existing terms only within this owned worker scope."""
        budget.consume("work")
        if isinstance(value, PatchBlank):
            if value.token not in blanks:
                budget.consume("states")
                budget.consume("bytes", 64)
                blanks[value.token] = BNode()
            return blanks[value.token]
        if isinstance(value, PatchLiteral):
            return literal_term(
                value.lexical, datatype=value.datatype, language=value.language
            )
        return value

    if witness.graph_selection is not None:
        node(witness.graph_selection)
    for quad in witness.quads:
        budget.consume("work")
        budget.consume("triples")
        graph.add((node(quad.subject), node(quad.predicate), node(quad.value)))
    return graph, blanks
