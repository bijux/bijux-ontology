"""Loss-aware compilation of one nonrecursive disjoint outgoing record shape."""

import json
from collections.abc import Mapping
from threading import Event
from typing import Literal, cast

from rdflib import BNode, URIRef
from rdflib.namespace import XSD

from bijux_ontology._json import object_value
from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclClosed,
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.inspection.reading import inspect_shapes
from bijux_ontology.shacl.inspection.types import ShapeInspection
from bijux_ontology.shex.preparation import CONTEXT, Unsupported, schemas
from bijux_ontology.shex.preparation import bindings as capture_bindings
from bijux_ontology.shex.types import ShexBinding, ShexSchema

from .types import InterchangeRemainder, ShapePreservation, ShapeTranslation

_KINDS = {
    "iri": "iri",
    "blank": "bnode",
    "literal": "literal",
    "blank_or_iri": "nonliteral",
}
_DATATYPES = (str(XSD.string), str(XSD.integer))
Definition = ShaclNodeShape | ShaclPropertyShape


def _map(
    values: tuple[ShexBinding, ...], operation: Operation
) -> tuple[tuple[ShexBinding, ...], URIRef]:
    """Require a nonempty explicit named map to exactly one selected shape."""
    if type(values) is not tuple:
        raise InputError("Interchange requires an immutable explicit shape map")
    captured = capture_bindings(values, operation)
    if not captured:
        raise ResourceError("An empty shape map cannot establish aligned target scope")
    seen: set[URIRef] = set()
    root = captured[0].shape
    for binding in captured:
        operation.consume("states")
        if not isinstance(binding.focus, URIRef) or not isinstance(
            binding.shape, URIRef
        ):
            raise ResourceError(
                "The common native fragment requires named focus and shape IRIs"
            )
        if binding.shape != root or binding.focus in seen:
            raise ResourceError("Use one root with distinct explicit focus bindings")
        seen.add(binding.focus)
    assert isinstance(root, URIRef)
    return captured, root


def _property(
    definition: ShaclPropertyShape, operation: Operation
) -> dict[str, object]:
    """Compile only one ordinary predicate's independent universal value rule."""
    path = definition.path
    if (
        not isinstance(path, RdfPath)
        or path.kind != "predicate"
        or not isinstance(path.node, URIRef)
    ):
        raise ResourceError(
            "Compound and inverse paths are outside the common fragment"
        )
    bound_node(path.node, operation)
    result: dict[str, object] = {
        "type": "TripleConstraint",
        "predicate": str(path.node),
        "min": 0,
        "max": -1,
    }
    value: dict[str, object] = {"type": "NodeConstraint"}
    for constraint in definition.constraints:
        operation.consume("work")
        if isinstance(constraint, ShaclCount):
            minimum, maximum = constraint.minimum or 0, constraint.maximum
            if maximum is not None and maximum < minimum:
                raise ResourceError("Contradictory bounds exceed the common fragment")
            result["min"], result["max"] = minimum, -1 if maximum is None else maximum
        elif isinstance(constraint, ShaclValue):
            if constraint.class_iri is not None:
                raise ResourceError(
                    "SHACL class closure has no exact raw-type ShEx translation"
                )
            if constraint.has_value is not ABSENT or constraint.in_values is not None:
                raise ResourceError(
                    "Existence and enumeration constraints are outside this fragment"
                )
            if constraint.datatype is not None:
                bound_text(constraint.datatype, operation)
                if constraint.datatype not in _DATATYPES:
                    raise ResourceError(
                        "Datatype is outside the qualified native scalar fragment"
                    )
                value["datatype"] = constraint.datatype
            if constraint.node_kind is not None:
                if constraint.node_kind not in _KINDS:
                    raise ResourceError(
                        "Disjunctive node kinds exceed the common fragment"
                    )
                value["nodeKind"] = _KINDS[constraint.node_kind]
        else:
            raise ResourceError(
                "Referenced or logical values exceed the common fragment"
            )
    if len(value) > 1:
        result["valueExpr"] = value
    operation.consume("bytes", json_bound(result, operation))
    return result


def _record(
    definitions: tuple[Definition, ...],
    root: URIRef,
    bindings: tuple[ShexBinding, ...],
    policy: Literal["exact", "pairs"],
    operation: Operation,
) -> tuple[dict[str, object], ShapePreservation]:
    """Reject overlaps and changed target selection instead of weakening them."""
    operation.consume("states", len(definitions))
    lookup = {definition.identifier: definition for definition in definitions}
    node = lookup.get(root)
    if not isinstance(node, ShaclNodeShape) or not node.properties:
        raise ResourceError("The common fragment requires one nonempty node record")
    if node.target_classes:
        raise ResourceError(
            "Class target discovery cannot be equated with an explicit shape map"
        )
    focus = {binding.focus for binding in bindings}
    targets = set(node.target_nodes)
    if policy == "pairs" and targets or policy == "exact" and targets != focus:
        raise ResourceError(
            "Authored target nodes differ from the declared map and policy"
        )
    closed = False
    for constraint in node.constraints:
        if not isinstance(constraint, ShaclClosed) or constraint.ignored_properties:
            raise ResourceError(
                "Node constraints and ignored predicates exceed the common fragment"
            )
        closed = constraint.enabled
    properties: list[dict[str, object]] = []
    predicates: list[URIRef] = []
    seen_predicates: set[URIRef] = set()
    datatypes: list[str] = []
    selected: set[URIRef | BNode] = {root}
    for ref in node.properties:
        operation.consume("work")
        operation.consume("states", 4)
        operation.consume("bytes", 256)
        definition = lookup.get(ref.identifier)
        if not isinstance(definition, ShaclPropertyShape):
            raise ResourceError(
                "Every direct property must have one supplied definition"
            )
        compiled = _property(definition, operation)
        predicate = URIRef(cast(str, compiled["predicate"]))
        if predicate in seen_predicates:
            raise ResourceError(
                "Overlapping predicates require distinct triple consumption"
            )
        selected.add(definition.identifier)
        seen_predicates.add(predicate)
        predicates.append(predicate)
        for constraint in definition.constraints:
            if isinstance(constraint, ShaclValue) and constraint.datatype is not None:
                datatypes.append(constraint.datatype)
        properties.append(compiled)
    if set(lookup) != selected:
        raise ResourceError("Additional definitions exceed the nonrecursive fragment")
    expression: dict[str, object] = (
        properties[0]
        if len(properties) == 1
        else {"type": "EachOf", "expressions": properties}
    )
    return {
        "type": "Shape",
        "closed": closed,
        "expression": expression,
    }, ShapePreservation(
        root,
        closed,
        policy,
        tuple(predicates),
        tuple(dict.fromkeys(datatypes)),
    )


def _finish(
    source: PreparedSource | ShexSchema,
    direction: Literal["shacl_to_shex", "shex_to_shacl"],
    shapes: PreparedSource | None,
    schema: ShexSchema | None,
    bindings: tuple[ShexBinding, ...],
    definitions: tuple[Definition, ...],
    inspection: ShapeInspection | None,
    preservation: ShapePreservation | None,
    remainder: list[InterchangeRemainder],
    issues: IssueCollector,
    operation: Operation,
    schema_imports: tuple[ShexSchema, ...] = (),
) -> ShapeTranslation:
    """Publish only complete compiled resources with their qualified shape metadata."""
    metadata = None
    try:
        if shapes is not None:
            metadata = SnapshotMetadata.capture(shapes, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    complete = (
        preservation is not None
        and schema is not None
        and metadata is not None
        and not issues
    )
    return ShapeTranslation._owned(
        source=source,
        direction=direction,
        shapes=shapes,
        schema=schema,
        schema_imports=schema_imports,
        bindings=bindings,
        definitions=definitions,
        inspection=inspection,
        preservation=preservation,
        shape_metadata=metadata,
        remainder=tuple(remainder),
        report=Report(
            "shape-interchange.compile",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="disjoint-nonrecursive-validation-fragment",
            children=() if inspection is None else (inspection.report,),
        ),
    )


def _failure(
    error: InputError,
    issues: IssueCollector,
    remainder: list[InterchangeRemainder],
    location: str,
) -> None:
    """Retain unsupported source obligations without manufacturing a target artifact."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error), location=location)
        return
    try:
        issues.append(
            Issue(
                "shape-interchange.unsupported"
                if isinstance(error, ResourceError | Unsupported)
                else "input.invalid",
                str(error),
                location,
                kind="unverified"
                if isinstance(error, ResourceError | Unsupported)
                else "input_error",
            )
        )
        issues.budget.consume("states")
        issues.budget.consume("bytes", 64)
        remainder.append(InterchangeRemainder(location, str(error)))
    except LimitExceeded as exhausted:
        issues.exhausted(str(exhausted), location=location)


def shacl_to_shex(
    source: PreparedSource,
    *,
    bindings: tuple[ShexBinding, ...],
    schema_iri: str,
    target_policy: Literal["exact", "pairs"] = "exact",
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ShapeTranslation:
    """Compile the selected common SHACL fragment, preserving its open/closed rule.

    Exact policy requires authored targetNode values to equal the supplied map.
    Pairs policy requires an untargeted shape and supplies that map explicitly.
    Unknown semantic RDF remains in the original inspection and blocks output.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    captured: tuple[ShexBinding, ...] = ()
    definitions: tuple[Definition, ...] = ()
    inspection = None
    schema = None
    shapes = None
    preservation = None
    remainder: list[InterchangeRemainder] = []
    try:
        if type(source) is not PreparedSource or target_policy not in (
            "exact",
            "pairs",
        ):
            raise InputError(
                "Use prepared SHACL and an explicit supported target policy"
            )
        bound_text(schema_iri, operation)
        absolute_iri(schema_iri)
        captured, root = _map(bindings, operation)
        inspection = inspect_shapes(
            source, (root,), graph_name=graph_name, budget=operation
        )
        retain_report(inspection.report, operation, issues)
        if not inspection.report.verified:
            raise ResourceError("Selected typed SHACL semantics are incomplete")
        definitions = inspection.definitions
        expression, preservation = _record(
            definitions, root, captured, target_policy, operation
        )
        document = {
            "@context": CONTEXT,
            "type": "Schema",
            "shapes": [
                {"type": "ShapeDecl", "id": str(root), "shapeExpr": expression},
            ],
        }
        operation.consume("bytes", json_bound(document, operation))
        schema = ShexSchema(
            schema_iri, json.dumps(document, ensure_ascii=True, separators=(",", ":"))
        )
        schemas(schema, None, operation)
        shapes = inspection.profile.source
    except InputError as error:
        _failure(error, issues, remainder, "shacl")
    return _finish(
        source,
        "shacl_to_shex",
        shapes,
        schema,
        captured,
        definitions,
        inspection,
        preservation,
        remainder,
        issues,
        operation,
    )


def shex_to_shacl(
    source: ShexSchema,
    *,
    bindings: tuple[ShexBinding, ...],
    target_policy: Literal["exact", "pairs"] = "exact",
    imports: Mapping[str, ShexSchema] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ShapeTranslation:
    """Compile a selected ShExJ record without replacing triple-consumption rules."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    captured: tuple[ShexBinding, ...] = ()
    definitions: tuple[Definition, ...] = ()
    shapes = None
    preservation = None
    remainder: list[InterchangeRemainder] = []
    schema_imports: tuple[ShexSchema, ...] = ()
    try:
        if not isinstance(source, ShexSchema) or target_policy not in (
            "exact",
            "pairs",
        ):
            raise InputError(
                "Use supplied ShExJ and an explicit supported target policy"
            )
        captured, root = _map(bindings, operation)
        supplied: dict[str, ShexSchema] = {}
        if imports is not None:
            if type(imports) is not dict:
                raise InputError("Interchange imports require a finite ordinary dict")
            for iri, resource in imports.items():
                operation.consume("states")
                bound_text(iri, operation)
                if not isinstance(resource, ShexSchema):
                    raise InputError(
                        "Interchange imports require supplied ShexSchema records"
                    )
                bound_text(resource.document_iri, operation)
                bound_text(resource.content, operation)
                supplied[iri] = resource
        document, closure, _ = schemas(source, supplied, operation)
        operation.consume("states", len(closure))
        schema_imports = tuple(supplied[iri] for iri in closure)
        declarations = cast(list[object], document["shapes"])
        selected = [
            object_value(item, location="ShapeDecl")
            for item in declarations
            if object_value(item, location="ShapeDecl")["id"] == str(root)
        ]
        if len(selected) != 1:
            raise ResourceError(
                "The exact requested root is not one supplied declaration"
            )
        shape = object_value(selected[0]["shapeExpr"], location="Shape")
        if shape.get("type") != "Shape" or set(shape) - {
            "type",
            "closed",
            "expression",
        }:
            raise ResourceError(
                "EXTRA, references or compound shape expressions exceed the fragment"
            )
        expr = object_value(shape.get("expression"), location="TripleExpression")
        if expr.get("type") == "EachOf":
            if (
                set(expr) - {"type", "expressions", "min", "max"}
                or expr.get("min", 1) != 1
                or expr.get("max", 1) != 1
            ):
                raise ResourceError(
                    "Repeated group consumption exceeds the common fragment"
                )
            parts = cast(list[object], expr["expressions"])
        else:
            parts = [expr]
        properties: list[ShaclPropertyShape] = []
        predicates: set[URIRef] = set()
        for raw in parts:
            operation.consume("work")
            operation.consume("states", 4)
            operation.consume("bytes", 256)
            item = object_value(raw, location="TripleConstraint")
            if item.get("type") != "TripleConstraint" or set(item) - {
                "type",
                "predicate",
                "valueExpr",
                "min",
                "max",
            }:
                raise ResourceError(
                    "Inverse or compound consumption exceeds the common fragment"
                )
            predicate = URIRef(cast(str, item["predicate"]))
            bound_node(predicate, operation)
            if predicate in predicates:
                raise ResourceError(
                    "Overlapping consumption has no independent SHACL translation"
                )
            predicates.add(predicate)
            value = object_value(
                item.get("valueExpr", {"type": "NodeConstraint"}),
                location="NodeConstraint",
            )
            if value.get("type") != "NodeConstraint" or set(value) - {
                "type",
                "nodeKind",
                "datatype",
            }:
                raise ResourceError(
                    "References, enumerations and semantic actions exceed the fragment"
                )
            native_kind = value.get("nodeKind")
            reverse = {v: k for k, v in _KINDS.items()}
            if native_kind is not None and native_kind not in reverse:
                raise ResourceError("Node kind exceeds the common fragment")
            datatype = cast(str | None, value.get("datatype"))
            if datatype is not None and datatype not in _DATATYPES:
                raise ResourceError("Datatype exceeds the common native fragment")
            minimum, maximum = (
                cast(int, item.get("min", 1)),
                cast(int, item.get("max", 1)),
            )
            if maximum != -1 and maximum < minimum:
                raise ResourceError("Contradictory bounds exceed the common fragment")
            properties.append(
                ShaclPropertyShape(
                    BNode(),
                    predicate,
                    (
                        ShaclCount(minimum, None if maximum == -1 else maximum),
                        ShaclValue(
                            datatype=datatype,
                            node_kind=cast(
                                Literal["iri", "blank", "literal", "blank_or_iri"]
                                | None,
                                reverse[native_kind]
                                if native_kind is not None
                                else None,
                            ),
                        ),
                    ),
                )
            )
        node = ShaclNodeShape(
            root,
            tuple(ShaclShapeRef(item.identifier) for item in properties),
            (ShaclClosed(cast(bool, shape.get("closed", False))),),
            target_nodes=tuple(cast(URIRef, b.focus) for b in captured)
            if target_policy == "exact"
            else (),
        )
        definitions = (node, *properties)
        _, preservation = _record(definitions, root, captured, target_policy, operation)
        shapes = compile_shapes(definitions, budget=operation)
    except InputError as error:
        _failure(error, issues, remainder, "shex")
    return _finish(
        source,
        "shex_to_shacl",
        shapes,
        source if isinstance(source, ShexSchema) else None,
        captured,
        definitions,
        None,
        preservation,
        remainder,
        issues,
        operation,
        schema_imports,
    )
