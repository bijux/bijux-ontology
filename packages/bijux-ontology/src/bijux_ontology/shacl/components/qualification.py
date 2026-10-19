"""Qualify authored component queries through maintained SHACL/SPARQL machinery."""

from dataclasses import replace

from pyparsing import ParseBaseException, ParseResults
from rdflib import BNode, Graph, Literal, URIRef, Variable
from rdflib.namespace import OWL, RDF, SH
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.parameters import parameter_name
from bijux_ontology.shacl.queries import _CASTS, _nodes, analyze_query

from .types import ComponentShapes, ComponentUse, ShaclComponent

_ROLES = {
    "generic": SH.validator,
    "node": SH.nodeValidator,
    "property": SH.propertyValidator,
}


def _prebinding(text: str, parameters: set[str], budget: Budget) -> None:
    """Check parsed variable roles after maintained PATH substitution.

    The backend's own helper also checks its supported pre-binding dialect. This
    parsed-tree guard covers every AS/subquery, including later occurrences that
    its first-match diagnostics do not inspect. No query grammar is implemented.
    """
    parsed = parseQuery(text)
    required = {"this", "value", *parameters}
    reserved = required | {"currentShape", "shapesGraph"}
    for part in _nodes(parsed, budget):
        if part.name in ("MinusGraphPattern", "InlineData", "ValuesClause"):
            raise InputError("SHACL pre-binding excludes MINUS and VALUES")
        if part.name == "Bind" and str(part["var"]) in reserved:
            raise InputError("A query cannot rebind a potentially pre-bound variable")
        if part.name == "vars" and "evar" in part and str(part["evar"]) in reserved:
            raise InputError(
                "A projection cannot rebind a potentially pre-bound variable"
            )
        if part.name == "SubSelect":
            # CompValue.get takes an evaluation flag, not a default value.
            projected = {
                str(item["var"])
                for item in (part["projection"] if "projection" in part else ())  # noqa: SIM401
                if "var" in item
            }
            if not required <= projected:
                raise InputError(
                    "Nested SELECT must project potentially pre-bound variables"
                )
    pending: list[object] = [parsed]
    while pending:
        budget.consume("work")
        item = pending.pop()
        if isinstance(item, Variable) and str(item) == "PATH":
            raise InputError("PATH is only supported in property predicate position")
        if isinstance(item, dict):
            budget.consume("states", len(item))
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, ParseResults)):
            budget.consume("states", len(item))
            pending.extend(item)


def _prefixes(graph: Graph, node: Node, budget: Budget) -> None:
    """Reject conflicting declarations in the maintained helper's prefix scope."""
    owners: list[Node] = [graph.identifier]
    for owner in graph.subjects(RDF.type, OWL.Ontology):
        budget.consume("work")
        budget.consume("states")
        owners.append(owner)
    for owner in graph.objects(node, SH.prefixes):
        budget.consume("work")
        budget.consume("states")
        owners.append(owner)
    namespaces: dict[str, str] = {}
    for owner in owners:
        for declaration in graph.objects(owner, SH.declare):
            budget.consume("work")
            for prefix in graph.objects(declaration, SH.prefix):
                budget.consume("work")
                bound_node(prefix, budget)
                for namespace in graph.objects(declaration, SH.namespace):
                    budget.consume("work")
                    budget.consume("states")
                    bound_node(namespace, budget)
                    key, iri = str(prefix), str(namespace)
                    if key in namespaces and namespaces[key] != iri:
                        raise InputError(
                            "Conflicting component query prefix declarations"
                        )
                    namespaces[key] = iri


def _qualify(
    compiled: ComponentShapes,
    jobs: list[tuple[ComponentUse, ShaclComponent, Node]],
    budget: Operation,
) -> ComponentShapes:
    """Keep core RDF useful when maintained optional qualification is unavailable."""
    issues = IssueCollector(budget)
    invalid = False
    checked = 0
    try:
        try:
            from pyshacl.errors import ReportableRuntimeError
            from pyshacl.helper.sparql_query_helper import SPARQLQueryHelper
            from pyshacl.shape import Shape
            from pyshacl.shapes_graph import ShapesGraph
        except ImportError as error:
            raise ResourceError(
                "Install bijux-ontology[shacl] to qualify component queries"
            ) from error
        assert compiled.source is not None
        graph = compiled.source._read(budget=budget)
        # The maintained helper binds namespaces in its local graph. Reserve the
        # ordinary defensive copy before giving it mutable namespace ownership.
        budget.consume("work", 4 * len(graph))
        budget.consume("states", len(graph))
        budget.consume("triples", len(graph))
        supplied = ShapesGraph(compiled.source.graph())
        for use, definition, _ in jobs:
            parameters: set[str] = set()
            for parameter in definition.parameters:
                budget.consume("states")
                bound_text(parameter.path, budget)
                name = parameter_name(parameter.path)
                bound_text(name, budget)
                parameters.add(name)
                for value in graph.objects(use.shape, URIRef(parameter.path)):
                    budget.consume("work")
                    bound_node(value, budget)
                    if isinstance(value, BNode):
                        issues.append(
                            Issue(
                                "shacl.component_unverified",
                                "Anonymous parameters lack a separately portable "
                                "data identity binding",
                                kind="unverified",
                            )
                        )
            for validator in definition.validators:
                budget.consume("work")
                node = next(
                    graph.objects(URIRef(definition.iri), _ROLES[validator.role])
                )
                property_role = (
                    validator.role == "property"
                    or validator.role == "generic"
                    and (use.shape, SH.path, None) in graph
                )
                path = graph.value(use.shape, SH.path) if property_role else None
                # An unused role is syntax-qualified with a symbolic predicate;
                # only explicit real uses receive execution requirements/coverage.
                if property_role and path is None:
                    path = URIRef("urn:bijux:ontology:component-path")
                if path is not None and not isinstance(path, URIRef | BNode):
                    raise InputError("A property shape needs a resource path")
                _prefixes(supplied.graph, node, budget)
                try:
                    shape: Shape = Shape(
                        supplied, use.shape, p=property_role, path=path
                    )
                    text = (
                        validator.ask if validator.ask is not None else validator.select
                    )
                    assert text is not None
                    bound_text(text, budget)
                    helper = SPARQLQueryHelper(shape, node, text)  # type: ignore[no-untyped-call]
                    helper.collect_prefixes()  # type: ignore[no-untyped-call]
                    _, concrete = helper.pre_bind_variables(  # type: ignore[no-untyped-call]
                        URIRef("urn:bijux:ontology:component-focus"),
                        valuenode=URIRef("urn:bijux:ontology:component-value"),
                        extravars=parameters,
                    )
                    analysis = analyze_query(shape, node, Literal(concrete), budget)
                    _prebinding(analysis.text, parameters, budget)
                except LimitExceeded:
                    raise
                except (
                    InputError,
                    ValueError,
                    ParseBaseException,
                    ReportableRuntimeError,
                    NotImplementedError,
                ) as error:
                    raise InputError(
                        "Component query violates maintained syntax/pre-binding support"
                    ) from error
                expected = "AskQuery" if validator.ask is not None else "SelectQuery"
                if (
                    analysis.form != expected
                    or validator.select is not None
                    and (
                        analysis.projection is None
                        or Variable("this") not in analysis.projection
                    )
                ):
                    raise InputError(
                        "Validator query form/projection does not match its role"
                    )
                for call in analysis.calls:
                    budget.consume("work")
                    if call.iri not in _CASTS:
                        issues.append(
                            Issue(
                                "shacl.component_unverified",
                                "Custom RDF functions require a separately "
                                "supported dependency profile",
                                kind="unverified",
                            )
                        )
                checked += 1
            if isinstance(use.shape, BNode):
                issues.append(
                    Issue(
                        "shacl.component_unverified",
                        "Anonymous root parameter execution lacks "
                        "named requirement attribution",
                        kind="unverified",
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ResourceError) as error:
        invalid = not isinstance(error, ResourceError)
        try:
            issues.append(
                Issue("shacl.component_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    query_report = Report(
        "shacl-component-queries",
        checked,
        tuple(issues),
        requested=max(1, checked),
        regime="maintained-shacl-sparql-query-qualification",
    )
    report = Report(
        "shacl-components",
        checked,
        children=(compiled.structural_report, query_report),
        regime="typed-shacl-component-compilation",
    )
    return replace(compiled, source=None if invalid else compiled.source, report=report)
