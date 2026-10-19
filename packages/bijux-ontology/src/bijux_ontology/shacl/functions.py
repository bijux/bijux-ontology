"""Explicit owned SHACL SPARQL catalogs and isolated maintained execution."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from pyparsing import ParseBaseException
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.sparql import SPARQLError
from rdflib.term import Identifier

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import copy_graph
from bijux_ontology.reports import Issue
from bijux_ontology.shacl.queries import _CASTS, analyze_query

if TYPE_CHECKING:
    from pyshacl.functions.shacl_function import SPARQLFunction
    from pyshacl.parameter import SHACLParameter
    from rdflib.plugins.sparql.parserutils import CompValue
    from rdflib.plugins.sparql.sparql import FrozenBindings


def _name(path: str) -> str:
    name = path.split("#", 1)[1] if "#" in path else path.rsplit("/", 1)[-1]
    try:
        parsed = parseQuery(f"SELECT ?{name} WHERE {{}}")
    except ParseBaseException as error:
        raise InputError(
            "Function parameter local name is not a SPARQL variable"
        ) from error
    if (
        len(parsed[1]["projection"]) != 1
        or str(parsed[1]["projection"][0]["var"]) != name
    ):
        raise InputError(
            "Function parameter path requires a SPARQL variable local name"
        )
    return name


@dataclass(frozen=True)
class ShaclFunctionParameter:
    """An RDF argument name, optional position and descriptive datatype."""

    path: str
    order: int | Decimal | None = None
    optional: bool = False
    datatype: str | None = None

    def __post_init__(self) -> None:
        absolute_iri(self.path)
        try:
            _name(self.path)
        except Exception as error:
            raise InputError(
                "Function parameter path has no supported variable name"
            ) from error
        if type(self.optional) is not bool:
            raise InputError("Function optional must be a boolean")
        if self.order is not None and (
            isinstance(self.order, bool)
            or not isinstance(self.order, (int, Decimal))
            or not Decimal(self.order).is_finite()
        ):
            raise InputError("Function order requires a finite integer or decimal")
        if self.datatype is not None:
            absolute_iri(self.datatype)


@dataclass(frozen=True)
class ShaclSparqlFunction:
    """Author the supported SELECT/ASK form without coercing RDF return terms."""

    iri: str
    parameters: tuple[ShaclFunctionParameter, ...] = ()
    select: str | None = None
    ask: str | None = None
    return_type: str | None = None

    def __post_init__(self) -> None:
        absolute_iri(self.iri)
        parameters = tuple(self.parameters)
        if any(not isinstance(item, ShaclFunctionParameter) for item in parameters):
            raise InputError("Function parameters require typed argument declarations")
        object.__setattr__(self, "parameters", parameters)
        bodies = tuple(body for body in (self.select, self.ask) if body is not None)
        if len(bodies) != 1 or not isinstance(bodies[0], str) or not bodies[0].strip():
            raise InputError(
                "A SPARQL function requires exactly one SELECT or ASK body"
            )
        if self.return_type is not None:
            absolute_iri(self.return_type)

    def graph(self) -> Graph:
        """Export independently owned RDF definitions for a reusable catalog."""
        graph = Graph()
        node = URIRef(self.iri)
        graph.add((node, RDF.type, SH.SPARQLFunction))
        graph.add(
            (
                node,
                SH.select if self.select is not None else SH.ask,
                Literal(self.select if self.select is not None else self.ask),
            )
        )
        if self.return_type is not None:
            graph.add((node, SH.returnType, URIRef(self.return_type)))
        for item in self.parameters:
            parameter = BNode()
            graph.add((node, SH.parameter, parameter))
            graph.add((parameter, SH.path, URIRef(item.path)))
            if item.order is not None:
                graph.add((parameter, SH.order, Literal(item.order)))
            if item.optional:
                graph.add((parameter, SH.optional, Literal(True)))
            if item.datatype is not None:
                graph.add((parameter, SH.datatype, URIRef(item.datatype)))
        return graph


class ShaclFunctionCatalog:
    """Own selected function roots and their supplied transitive definitions.

    Trust authorizes only these SPARQL functions. Constraint queries require their
    separate trusted_sparql option; targets, rules and JavaScript remain separate.
    """

    __slots__ = ("_graph", "_selected", "_trusted")

    def __init__(
        self, graph: Graph, *, selected: Sequence[str], trusted: bool = False
    ) -> None:
        if (
            not isinstance(graph, Graph)
            or isinstance(graph, Dataset)
            or type(trusted) is not bool
        ):
            raise InputError(
                "A function catalog requires an RDF graph and explicit trust"
            )
        if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes)):
            raise InputError("Function selections require an IRI sequence")
        names = tuple(dict.fromkeys(absolute_iri(iri) for iri in selected))
        if not names:
            raise InputError("A function catalog requires at least one selected root")
        self._selected = names
        self._trusted = trusted
        self._graph = copy_graph(graph)

    @property
    def selected(self) -> tuple[str, ...]:
        """Return the explicit, immutable catalog roots."""
        return self._selected

    @property
    def trusted(self) -> bool:
        """State independent authorization for this owned function catalog."""
        return self._trusted

    def graph(self) -> Graph:
        """Export definitions without exposing the catalog's mutable graph ownership."""
        return copy_graph(self._graph, standardize_apart=False)

    @classmethod
    def from_functions(
        cls,
        definitions: Sequence[ShaclSparqlFunction],
        *,
        selected: Sequence[str] | None = None,
        trusted: bool = False,
    ) -> ShaclFunctionCatalog:
        """Build a catalog from typed authoring; omitted selection chooses all roots."""
        graph = Graph()
        names = []
        for function in definitions:
            if not isinstance(function, ShaclSparqlFunction):
                raise InputError("Expected typed SPARQL function definitions")
            graph += function.graph()
            names.append(function.iri)
        return cls(
            graph, selected=names if selected is None else selected, trusted=trusted
        )


@dataclass(frozen=True)
class FunctionPlan:
    """Qualified selected dependency closure, distinct from actual execution counts."""

    graph: Graph
    names: tuple[str, ...] = ()
    issues: tuple[Issue, ...] = ()


def _signature(function: SPARQLFunction, graph: Graph) -> None:
    """Validate parameter identities and unambiguous per-function positions."""
    names = []
    orders: list[Decimal | None] = []
    for parameter in function.parameters:
        paths = tuple(graph.objects(parameter.node, SH.path))
        if len(paths) != 1 or not isinstance(paths[0], URIRef):
            raise InputError("Function parameters require exactly one IRI sh:path")
        absolute_iri(str(paths[0]))
        names.append(_name(str(paths[0])))
        if parameter.datatype is not None and not isinstance(
            parameter.datatype, URIRef
        ):
            raise InputError("Function parameter datatype must be an IRI")
        if parameter.datatype is not None:
            absolute_iri(str(parameter.datatype))
        if parameter.nodekind is not None and not isinstance(
            parameter.nodekind, URIRef
        ):
            raise InputError("Function parameter nodeKind must be an IRI")
        for raw_order in graph.objects(parameter.node, SH.order):
            if not isinstance(raw_order, Literal) or isinstance(raw_order.value, bool):
                raise InputError("Function order must be a numeric RDF literal")
        order = parameter.param_order
        if order is not None and not order.is_finite():
            raise InputError("Function parameter order must be finite")
        orders.append(order)
    if len(set(names)) != len(names):
        raise InputError("Function parameter local names must be unique")
    if any(order is not None for order in orders):
        normalized = [Decimal(0) if order is None else order for order in orders]
        if len(set(normalized)) != len(normalized):
            raise InputError("Function parameter positions are ambiguous")
    if function.rtype is not None:
        if not isinstance(function.rtype, URIRef):
            raise InputError("Function returnType must be an IRI")
        absolute_iri(str(function.rtype))


def prepare_functions(
    catalog: ShaclFunctionCatalog | None, limits: Limits
) -> FunctionPlan:
    """Qualify parsed dependencies without selecting dormant adjacent functions."""
    if catalog is None:
        return FunctionPlan(Graph())
    graph = catalog.graph()
    if not catalog.trusted:
        return FunctionPlan(
            graph,
            issues=(
                Issue(
                    "shacl.function_untrusted",
                    "Function catalogs require explicit trusted=True",
                    "functions",
                    kind="unverified",
                ),
            ),
        )
    try:
        from pyshacl.errors import (
            ConstraintLoadError,
            ReportableRuntimeError,
            ShapeLoadError,
        )
        from pyshacl.functions.shacl_function import SPARQLFunction
        from pyshacl.shapes_graph import ShapesGraph
    except ImportError:
        return FunctionPlan(
            graph,
            issues=(
                Issue(
                    "shacl.function_unavailable",
                    "Install bijux-ontology[shacl] for function catalogs",
                    "functions",
                    kind="unverified",
                ),
            ),
        )
    budget = Budget(limits)
    issues = IssueCollector(budget)
    states: dict[URIRef, int] = {}
    ordered = []
    pending = [(URIRef(iri), False, 0) for iri in reversed(catalog.selected)]
    supplied = ShapesGraph(graph)
    location = "functions"
    try:
        budget.consume("triples", len(graph))
        while pending:
            node, exiting, depth = pending.pop()
            location = str(node)
            budget.consume("work")
            if exiting:
                states[node] = 2
                ordered.append(str(node))
                continue
            if states.get(node) == 2:
                continue
            if states.get(node) == 1:
                issues.append(
                    Issue(
                        "shacl.function_recursive",
                        "Recursive function catalogs are unsupported",
                        location,
                        kind="unverified",
                    )
                )
                continue
            budget.consume("states")
            if depth >= limits.max_depth:
                raise LimitExceeded("Function dependency depth exceeds max_depth")
            if node in _CASTS or (node, RDF.type, SH.SPARQLFunction) not in graph:
                issues.append(
                    Issue(
                        "shacl.function_unavailable",
                        "Selected function has no supported SPARQL implementation",
                        location,
                        kind="unverified",
                    )
                )
                states[node] = 2
                continue
            parameter_count = len(tuple(graph.objects(node, SH.parameter)))
            budget.consume("work", parameter_count)
            budget.consume("states", parameter_count)
            function = SPARQLFunction(node, supplied)  # type: ignore[no-untyped-call]
            _signature(function, graph)
            text = function.select if function.select is not None else function.ask
            analysis = analyze_query(function, node, text, budget)
            expected = "SelectQuery" if function.select is not None else "AskQuery"
            if (
                analysis.form != expected
                or expected == "SelectQuery"
                and (analysis.projection is None or len(analysis.projection) != 1)
            ):
                raise InputError(
                    "Function requires matching ASK or a single-variable SELECT"
                )
            graph.set(
                (
                    node,
                    SH.select if function.select is not None else SH.ask,
                    Literal(analysis.text),
                )
            )
            states[node] = 1
            pending.append((node, True, depth))
            pending.extend(
                (call.iri, False, depth + 1)
                for call in analysis.calls
                if call.iri not in _CASTS
            )
    except (
        InputError,
        ConstraintLoadError,
        ReportableRuntimeError,
        ShapeLoadError,
        InvalidOperation,
    ) as error:
        if isinstance(error, LimitExceeded):
            issues.exhausted(str(error), location=location)
        else:
            issues.append(
                Issue(
                    "shacl.function_malformed", str(error), location, kind="unverified"
                )
            )
    return FunctionPlan(graph, tuple(dict.fromkeys(ordered)), tuple(issues))


@contextmanager
def register_functions(
    graph: Graph,
    names: Sequence[str],
    data: Graph,
    limits: Limits,
) -> Iterator[dict[str, int]]:
    """Register maintained SPARQL bodies only within the already isolated worker."""
    from pyshacl.functions.shacl_function import SPARQLFunction
    from pyshacl.graph_abstraction import DataGraph
    from pyshacl.shapes_graph import ShapesGraph

    budget = Budget(limits)
    calls = dict.fromkeys(names, 0)
    depth = 0

    class OwnedFunction(SPARQLFunction):
        """Keep maintained queries behind owned argument and error boundaries."""

        def get_params_in_order(self) -> list[SHACLParameter]:
            """Apply AF defaults without mutating reusable parameter declarations."""
            if any(parameter.param_order is not None for parameter in self.parameters):
                return sorted(
                    self.parameters,
                    key=lambda parameter: (
                        Decimal(0)
                        if parameter.param_order is None
                        else parameter.param_order
                    ),
                )
            return sorted(self.parameters, key=lambda parameter: parameter.localname)

        def execute_from_sparql(
            self, expression: CompValue, context: FrozenBindings
        ) -> URIRef | BNode | Literal:
            """Bind supplied RDF arguments and retain results or SPARQL errors."""
            nonlocal depth
            budget.consume("work")
            if depth >= limits.max_depth:
                raise LimitExceeded("Function invocation depth exceeds max_depth")
            calls[str(self.node)] += 1
            depth += 1
            try:
                parameters = self.get_params_in_order()
                arguments: Sequence[Identifier] = ()
                if "expr" in expression:
                    arguments = expression["expr"]
                if len(arguments) > len(parameters) or any(
                    not parameter.optional for parameter in parameters[len(arguments) :]
                ):
                    raise SPARQLError("Function arguments do not satisfy the signature")
                bindings = {
                    parameters[index].localname: context[value]
                    for index, value in enumerate(arguments)
                }
                effective_graph = context.ctx.graph
                if effective_graph is None:
                    raise SPARQLError("Function has no effective data graph")
                try:
                    result = (
                        self.execute_ask(effective_graph, bindings)
                        if self.ask
                        else self.execute_select(effective_graph, bindings)
                    )
                except KeyError as error:
                    raise SPARQLError("Function result variable is unbound") from error
                if not isinstance(result, URIRef | BNode | Literal):
                    raise SPARQLError("Function returned no RDF term")
                return result
            finally:
                depth -= 1

    supplied = ShapesGraph(graph)
    effective = DataGraph.from_rdflib(data)
    applied = []
    try:
        for name in names:
            function = OwnedFunction(URIRef(name), supplied)  # type: ignore[no-untyped-call]
            applied.append(function)
            function.apply(effective)
        yield calls
    finally:
        for function in reversed(applied):
            function.unapply(effective)
