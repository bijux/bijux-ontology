"""A narrow finite SHACL property/node plan adapter, not shape validation."""

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SH, XSD
from rdflib.term import Node

from bijux_ontology.data.projection.types import ReadField
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.path_traversal import finite_path_steps
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.paths import decode_path

from .types import RecordField, RecordPlan

_METADATA = frozenset(
    (RDF.type, RDFS.label, RDFS.comment, SH.name, SH.description, SH.order)
)


def record_plan_from_shape(
    shapes: PreparedSource,
    root: URIRef | BNode,
    *,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> RecordPlan:
    """Project property/node paths and counts from one explicitly selected shape.

    Only nonrecursive sh:property, sh:node, finite sequence/inverse paths and
    integer min/max counts are supported. Other shape obligations are unknown;
    this operation never evaluates SHACL targets or establishes conformance.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Shape plan limits conflict with the operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    fields: tuple[RecordField, ...] = ()
    finished = False
    checked = 0
    try:
        if not isinstance(shapes, PreparedSource) or not isinstance(
            root, URIRef | BNode
        ):
            raise InputError("Shape plans require an owned source and a resource root")
        if shapes.union or shapes.graph_fidelity != "complete":
            raise InputError("Shape extraction requires one complete physical graph")
        if isinstance(graph_name, BNode) and not any(
            name is graph_name for name in shapes._graph_names(budget=operation)
        ):
            raise InputError("Blank shape graph requires exact snapshot ownership")
        contexts = shapes._contexts(graph_name, budget=operation)
        _, _, graph = next(contexts)
        if next(contexts, None) is not None:
            raise InputError("Shape extraction requires one physical graph")
        if isinstance(root, BNode):
            owned = False
            for triple in graph:
                operation.consume("work")
                for node in triple:
                    bound_node(node, operation)
                    owned = owned or node is root
            if not owned:
                raise InputError("Anonymous shape root requires exact source ownership")

        def values(node: Node, predicate: URIRef) -> tuple[Node, ...]:
            """Capture one actual finite shape predicate inventory."""
            result = []
            for value in graph.objects(node, predicate):
                operation.consume("work")
                operation.consume("states")
                operation.consume("bytes", 128)
                bound_node(value, operation)
                result.append(value)
            return tuple(result)

        def allowed(node: Node, predicates: frozenset[URIRef]) -> None:
            """Withhold plan completeness for obligations outside this adapter."""
            for _, predicate, value in graph.triples((node, None, None)):
                operation.consume("work")
                bound_node(predicate, operation)
                bound_node(value, operation)
                if predicate not in predicates | _METADATA:
                    raise InputError(
                        "Shape obligation is outside the property/node plan profile"
                    )

        def count(node: Node, predicate: URIRef, default: int | None) -> int | None:
            """Read checked native integers without bool/string coercion."""
            found = values(node, predicate)
            if not found:
                return default
            if len(found) != 1 or not isinstance(found[0], Literal):
                raise InputError("Shape cardinality requires one integer literal")
            literal = found[0]
            if literal.datatype is None or datatype_relation(
                str(literal.datatype), str(XSD.integer)
            ) not in ("equal", "subset"):
                raise InputError("Shape cardinality requires an integer datatype")
            result = check_literal(literal, budget=operation)
            issues.extend(result.issues)
            if not result.verified:
                raise InputError("Shape cardinality lexical form is not established")
            if len(literal) > 20:
                raise LimitExceeded(
                    "Shape cardinality exceeds the bounded native integer profile"
                )
            return int(str(literal))

        def walk(node: Node, active: frozenset[Node]) -> tuple[RecordField, ...]:
            """Expand only explicitly connected finite property and node shapes."""
            operation.consume("work")
            operation.consume("states")
            if node in active:
                raise InputError("Recursive shape extraction is not supported")
            if len(active) >= min(operation.limits.max_depth, 64):
                raise LimitExceeded("Shape plan exceeds the bounded description depth")
            allowed(node, frozenset((SH.property,)))
            result = []
            for child in values(node, SH.property):
                if not isinstance(child, URIRef | BNode):
                    raise InputError("Property shape requires a resource")
                allowed(child, frozenset((SH.path, SH.minCount, SH.maxCount, SH.node)))
                paths = values(child, SH.path)
                if len(paths) != 1:
                    raise InputError("Property shape requires exactly one path")
                path = decode_path(graph, paths[0], budget=operation, raise_limits=True)
                if (
                    path.limitation is not None
                    or finite_path_steps(path, operation) is None
                ):
                    raise InputError(
                        "Shape path is outside the finite sequence/inverse profile"
                    )
                nested = values(child, SH.node)
                if (
                    len(nested) > 1
                    or nested
                    and not isinstance(nested[0], URIRef | BNode)
                ):
                    raise InputError(
                        "Extraction supports one resource-valued child shape"
                    )
                minimum = count(child, SH.minCount, 0)
                assert minimum is not None
                field = ReadField(
                    path.readable,
                    path,
                    mode="many",
                    minimum=minimum,
                    maximum=count(child, SH.maxCount, None),
                )
                children = walk(nested[0], active | {node, child}) if nested else ()
                operation.consume("states")
                operation.consume("bytes", 128)
                result.append(RecordField(field, children))
            if not result:
                raise InputError("Shape plan requires a nonempty property selection")
            return tuple(result)

        fields = walk(root, frozenset())
        checked = 1
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("extraction.shape_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "data.extraction_shape_plan",
        checked,
        tuple(issues),
        requested=1,
        complete=finished,
        regime="finite-shacl-path-plan",
    )
    return RecordPlan(fields if report.verified else (), report)
