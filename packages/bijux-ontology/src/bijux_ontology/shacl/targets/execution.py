"""Resolve trusted maintained targets once inside the existing owned worker."""

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.targets.preparation import _definitions, _objects, _roots


def _parameters(
    shapes: Graph,
    declarations: tuple[tuple[Node, Node, tuple[Node, ...]], ...],
    budget: Budget,
) -> None:
    """Execute ordinary maintained Core constraints on actual parameter bindings."""
    import pyshacl
    from pyshacl.parameter import SHACLParameter
    from pyshacl.shapes_graph import ShapesGraph

    guard = Graph()
    supplied = ShapesGraph(shapes)
    for declaration, _, parameters in declarations:
        if not parameters:
            continue
        root = BNode()
        for triple in (
            (root, RDF.type, SH.NodeShape),
            (root, SH.targetNode, declaration),
        ):
            budget.consume("triples")
            guard.add(triple)
        for parameter in parameters:
            maintained = SHACLParameter(supplied, parameter)
            constraint = BNode()
            triples = [
                (root, SH.property, constraint),
                (constraint, SH.path, maintained.path()),  # type: ignore[no-untyped-call]
                (constraint, SH.maxCount, Literal(1)),
            ]
            if not maintained.optional:
                triples.append((constraint, SH.minCount, Literal(1)))
            for predicate in (SH.datatype, SH["class"], SH.nodeKind):
                for value in _objects(shapes, parameter, predicate, budget):
                    triples.append((constraint, predicate, value))
            for triple in triples:
                budget.consume("work")
                budget.consume("triples")
                guard.add(triple)
    if guard:
        conforms, _, _ = pyshacl.validate(
            shapes,
            shacl_graph=guard,
            advanced=False,
            js=False,
            inference="none",
            do_owl_imports=False,
            inplace=False,
            max_validation_depth=budget.limits.max_depth,
        )
        if not conforms:
            raise InputError("Trusted target parameters fail maintained Core guards")


def materialize_targets(
    data: Graph,
    shapes: Graph,
    ontology: Graph | None,
    limits: Limits,
    *,
    budget: Budget | None = None,
) -> None:
    """Replace complete maintained target results with actual Core target nodes.

    This helper runs only inside the existing isolated worker and registered
    function scope. No second target query runs during validation or coverage.
    Any limit or guard failure prevents the caller from returning conformance.
    Maintained query internals are protected by that worker's actual deadline.
    """
    from pyshacl import Validator
    from pyshacl.graph_abstraction import DataGraph
    from pyshacl.shape import Shape
    from pyshacl.target import SPARQLTargetType

    budget = budget if budget is not None else Budget(limits)
    validator = Validator(
        DataGraph.from_rdflib(data),
        shacl_graph=shapes,
        ont_graph=ontology,
        options={"inference": "none", "advanced": False, "use_js": False},
    )
    effective = validator.data_graph
    if ontology is not None:
        effective = validator.mix_in_ontology()  # type: ignore[no-untyped-call]
    supplied = validator.shacl_graph
    roots = _roots(shapes, budget)
    applied: set[Node] = set()
    for root in roots:
        budget.consume("work")
        bound_node(root, budget)
        if not isinstance(root, (URIRef, BNode)):
            raise InputError("Target roots must be resources")
        shape = Shape(supplied, root)
        if shape.deactivated:
            shapes.remove((root, SH.target, None))
            continue
        declarations = _definitions(shapes, root, budget)
        _parameters(shapes, declarations, budget)
        for declaration, definition, _ in declarations:
            if definition != declaration and definition not in applied:
                SPARQLTargetType(definition, supplied).apply()  # type: ignore[no-untyped-call]
                applied.add(definition)
        shape.set_advanced(True)  # type: ignore[no-untyped-call]
        nodes = shape.focus_nodes(effective)  # type: ignore[no-untyped-call]
        for node in nodes:
            budget.consume("work")
            bound_node(node, budget)
            budget.consume("states")
            budget.consume("triples")
            shapes.add((root, SH.targetNode, node))
        shapes.remove((root, SH.target, None))
