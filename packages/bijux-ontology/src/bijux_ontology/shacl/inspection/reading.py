"""Project a finite selected SHACL subset into the shared authoring records."""

from collections.abc import Sequence
from typing import Literal as Kind

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SH, XSD
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.roles.classification import bound_node
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import literal_datatype
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.types import (
    ShaclComparison,
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclQualifiedCount,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.paths import decode_path
from bijux_ontology.shacl.profiles.extraction import extract_profile

from .types import ShapeInspection, ShapeRemainder

_KINDS: dict[
    Node,
    Kind[
        "iri", "blank", "literal", "blank_or_iri", "blank_or_literal", "iri_or_literal"
    ],
] = {
    SH.IRI: "iri",
    SH.BlankNode: "blank",
    SH.Literal: "literal",
    SH.BlankNodeOrIRI: "blank_or_iri",
    SH.BlankNodeOrLiteral: "blank_or_literal",
    SH.IRIOrLiteral: "iri_or_literal",
}
_COMPARISONS: dict[
    URIRef, Kind["equals", "disjoint", "less_than", "less_than_or_equals"]
] = {
    SH.equals: "equals",
    SH.disjoint: "disjoint",
    SH.lessThan: "less_than",
    SH.lessThanOrEquals: "less_than_or_equals",
}
_ANNOTATIONS = frozenset(
    (
        RDFS.label,
        RDFS.comment,
        SH.name,
        SH.description,
        SH.order,
        SH.group,
        SH.defaultValue,
        SH.prefixes,
    )
)


def _resource(value: Node) -> URIRef | BNode:
    """Keep original shape identities without resolving labels across snapshots."""
    if not isinstance(value, URIRef | BNode):
        raise InputError("A shape reference requires an IRI or owned blank")
    return value


def _constant(value: Node) -> URIRef | Literal:
    """Qualify the authoring subset's named or literal constants."""
    if not isinstance(value, URIRef | Literal):
        raise InputError("Anonymous constants are outside typed shape projection")
    return value


class _Reader:
    """Read bounded original assertions without expanding recursive references."""

    def __init__(self, graph: Graph, budget: Budget) -> None:
        self.graph = graph
        self.budget = budget

    def values(self, node: Node, predicate: URIRef) -> tuple[Node, ...]:
        """Capture exact parameter terms under the caller's cumulative allowance."""
        values = []
        for value in self.graph.objects(node, predicate):
            self.budget.consume("work")
            self.budget.consume("states")
            bound_node(value, self.budget)
            values.append(value)
        return tuple(values)

    def one(self, node: Node, predicate: URIRef) -> Node | None:
        """Reject ambiguous singleton parameters instead of selecting one."""
        values = self.values(node, predicate)
        if len(values) > 1:
            raise InputError("A typed singleton SHACL parameter has multiple values")
        return values[0] if values else None

    def count(self, node: Node, predicate: URIRef) -> int | None:
        """Read valid bounded nonnegative integer counts without normalization."""
        value = self.one(node, predicate)
        if value is None:
            return None
        if not isinstance(value, Literal) or literal_datatype(value) != XSD.integer:
            raise InputError("SHACL counts require integer literals")
        if not check_literal(value, budget=self.budget).verified:
            raise InputError("SHACL count has invalid integer syntax")
        if len(value) > min(4096, self.budget.limits.max_bytes):
            raise LimitExceeded("SHACL count exceeds bounded integer representation")
        result = int(str(value))
        if result < 0:
            raise InputError("SHACL count cannot be negative")
        return result

    def constraints(self, node: URIRef | BNode) -> tuple[ShaclConstraint, ...]:
        """Project only supported constraint parameters, preserving conjunction."""
        result: list[ShaclConstraint] = []
        for predicate in (SH.datatype, SH["class"]):
            parameters = self.values(node, predicate)
            if predicate == SH.datatype and len(parameters) > 1:
                raise InputError("A datatype constraint requires one parameter")
            for value in parameters:
                if not isinstance(value, URIRef):
                    raise InputError("SHACL datatype and class parameters require IRIs")
                result.append(
                    ShaclValue(datatype=str(value))
                    if predicate == SH.datatype
                    else ShaclValue(class_iri=str(value))
                )
        kind = self.one(node, SH.nodeKind)
        if kind is not None:
            if kind not in _KINDS:
                raise InputError("Unknown SHACL node kind")
            result.append(ShaclValue(node_kind=_KINDS[kind]))
        for value in self.values(node, SH.hasValue):
            result.append(ShaclValue(has_value=_constant(value)))
        allowed = self.one(node, SH["in"])
        if allowed is not None:
            result.append(
                ShaclValue(
                    in_values=tuple(
                        _constant(value)
                        for value in iter_rdf_list(
                            self.graph,
                            allowed,
                            max_items=self.budget.limits.max_states,
                            budget=self.budget,
                        )
                    )
                )
            )
        minimum, maximum = self.count(node, SH.minCount), self.count(node, SH.maxCount)
        if minimum is not None or maximum is not None:
            result.append(ShaclCount(minimum, maximum))
        for predicate, operator in _COMPARISONS.items():
            for value in self.values(node, predicate):
                if not isinstance(value, URIRef):
                    raise InputError("Property comparison requires an IRI")
                result.append(ShaclComparison(operator, str(value)))
        for value in self.values(node, SH.node):
            result.append(ShaclShapeRef(_resource(value)))
        logical: tuple[tuple[URIRef, Kind["and", "or", "xone"]], ...] = (
            (SH["and"], "and"),
            (SH["or"], "or"),
            (SH.xone, "xone"),
        )
        for predicate, logical_operator in logical:
            for head in self.values(node, predicate):
                refs = tuple(
                    ShaclShapeRef(_resource(value))
                    for value in iter_rdf_list(
                        self.graph,
                        head,
                        max_items=self.budget.limits.max_states,
                        budget=self.budget,
                    )
                )
                if not refs:
                    raise InputError(
                        "Empty logical lists are outside the authoring subset"
                    )
                result.append(ShaclLogical(logical_operator, refs))
        for value in self.values(node, SH["not"]):
            result.append(ShaclLogical("not", (ShaclShapeRef(_resource(value)),)))
        qualified = self.one(node, SH.qualifiedValueShape)
        if qualified is None and any(
            self.values(node, p)
            for p in (
                SH.qualifiedMinCount,
                SH.qualifiedMaxCount,
                SH.qualifiedValueShapesDisjoint,
            )
        ):
            raise InputError("Qualified parameters require a referenced shape")
        if qualified is not None:
            qminimum = self.count(node, SH.qualifiedMinCount)
            qmaximum = self.count(node, SH.qualifiedMaxCount)
            if qminimum is None and qmaximum is None:
                raise InputError("Qualified authoring requires a count bound")
            disjoint = self.one(node, SH.qualifiedValueShapesDisjoint)
            if disjoint is not None and (
                not isinstance(disjoint, Literal)
                or literal_datatype(disjoint) != XSD.boolean
                or str(disjoint) not in ("true", "false", "1", "0")
            ):
                raise InputError("Qualified disjointness requires a boolean literal")
            result.append(
                ShaclQualifiedCount(
                    ShaclShapeRef(_resource(qualified)),
                    qminimum,
                    qmaximum,
                    str(disjoint) in ("true", "1"),
                )
            )
        return tuple(result)


_MANAGED = frozenset(
    (
        RDF.type,
        SH.path,
        SH.property,
        SH.targetClass,
        SH.targetNode,
        SH.datatype,
        SH["class"],
        SH.nodeKind,
        SH.hasValue,
        SH["in"],
        SH.minCount,
        SH.maxCount,
        SH.node,
        SH["and"],
        SH["or"],
        SH.xone,
        SH["not"],
        SH.qualifiedValueShape,
        SH.qualifiedMinCount,
        SH.qualifiedMaxCount,
        SH.qualifiedValueShapesDisjoint,
        *_COMPARISONS,
    )
)


def inspect_shapes(
    shapes: PreparedSource,
    roots: Sequence[str | URIRef | BNode],
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShapeInspection:
    """Read the supported selected authoring subset and retain semantic remainder.

    This is a typed projection, not validation or a claim about satisfiability.
    Original RDF remains authoritative, including exact spellings and extensions.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Inspection limits conflict with the supplied budget")
    operation = budget if budget is not None else Budget(limits)
    profile = extract_profile(shapes, roots, graph_name=graph_name, budget=operation)
    issues = IssueCollector(operation)
    definitions: list[ShaclNodeShape | ShaclPropertyShape] = []
    remainder: list[ShapeRemainder] = []
    try:
        graph = shapes._read(graph_name, budget=operation)
        reader = _Reader(graph, operation)
        operation.consume("states", len(profile.roots))
        nodes = dict.fromkeys(profile.roots)
        for edge in profile.dependencies:
            operation.consume("work")
            operation.consume("states")
            nodes[edge.dependency] = None
            nodes[edge.shape] = None
        for node in nodes:
            operation.consume("work")
            operation.consume("states")
            try:
                constraints = reader.constraints(node)
                path = reader.one(node, SH.path)
                if path is not None:
                    decoded = decode_path(
                        graph, path, budget=operation, raise_limits=True
                    )
                    definition: ShaclNodeShape | ShaclPropertyShape = (
                        ShaclPropertyShape(node, decoded, constraints)
                    )
                else:
                    if (node, RDF.type, SH.PropertyShape) in graph:
                        raise InputError("A property shape requires its path")
                    if any(
                        isinstance(
                            c, ShaclCount | ShaclComparison | ShaclQualifiedCount
                        )
                        for c in constraints
                    ):
                        raise InputError(
                            "A property constraint requires a property shape"
                        )
                    classes = reader.values(node, SH.targetClass)
                    if any(not isinstance(x, URIRef) for x in classes):
                        raise InputError("Class targets require IRIs")
                    definition = ShaclNodeShape(
                        node,
                        tuple(
                            ShaclShapeRef(_resource(x))
                            for x in reader.values(node, SH.property)
                        ),
                        constraints,
                        tuple(str(x) for x in classes),
                        tuple(_constant(x) for x in reader.values(node, SH.targetNode)),
                    )
                definitions.append(definition)
                for _, predicate, value in graph.triples((node, None, None)):
                    operation.consume("work")
                    operation.consume("states")
                    bound_node(predicate, operation)
                    bound_node(value, operation)
                    if (
                        predicate not in _MANAGED
                        and predicate not in _ANNOTATIONS
                        or predicate == RDF.type
                        and value not in (SH.NodeShape, SH.PropertyShape)
                        or isinstance(definition, ShaclPropertyShape)
                        and predicate in (SH.property, SH.targetClass, SH.targetNode)
                    ):
                        assert isinstance(predicate, URIRef)
                        remainder.append(ShapeRemainder(node, predicate, value))
            except LimitExceeded:
                raise
            except InputError:
                issues.append(
                    Issue(
                        "shacl.shape_projection_unverified",
                        "A selected shape is outside the supported "
                        "typed authoring subset",
                        kind="unverified",
                    )
                )
        if remainder:
            issues.append(
                Issue(
                    "shacl.shape_projection_unverified",
                    "Selected semantic assertions remain outside the typed projection",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "shacl-shape-inspection",
        len(definitions),
        tuple(issues),
        requested=max(1, len(definitions)),
        children=(profile.structural_report,),
        regime="selected-typed-shape-projection",
    )
    return ShapeInspection(
        shapes, graph_name, profile, tuple(definitions), tuple(remainder), report
    )
