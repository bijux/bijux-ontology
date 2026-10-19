"""Translate a finite inspected Core subset without inventing serializer facts."""

from collections.abc import Sequence
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, SH, XSD
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
    ValueKind,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, term_key
from bijux_ontology.shacl.authoring.types import (
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclValue,
)
from bijux_ontology.shacl.inspection.reading import inspect_shapes
from bijux_ontology.shacl.inspection.types import ShapeInspection

from .types import ShapeContractImport, ShapeFieldBinding, UntranslatedShapeConstraint

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
        SH.targetClass,
        SH.targetNode,
        SH.targetSubjectsOf,
        SH.targetObjectsOf,
    )
)
_KIND_BRANCHES: dict[str, tuple[ValueKind, ...]] = {
    "iri": ("iri",),
    "literal": ("literal",),
    "blank_or_iri": ("object",),
    "iri_or_literal": ("iri", "literal"),
}
Definition = ShaclNodeShape | ShaclPropertyShape
Covered = set[tuple[Node, URIRef]]


def _capture(
    values: Sequence[ShapeFieldBinding], budget: Budget
) -> tuple[ShapeFieldBinding, ...]:
    """Capture finite sidecars with one count and precharged owned references."""
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), str | bytes
    ):
        raise InputError("Shape field bindings require a finite sequence")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            binding = values[index]
        except IndexError as error:
            raise InputError(
                "Shape binding inventory changed during capture"
            ) from error
        if not isinstance(binding, ShapeFieldBinding):
            raise InputError("Expected typed ShapeFieldBinding records")
        bound_node(binding.shape, budget)
        if isinstance(binding.shape, URIRef):
            absolute_iri(binding.shape)
        bound_text(binding.name, budget)
        if binding.key is not None:
            bound_text(binding.key, budget)
        result.append(
            ShapeFieldBinding(
                binding.shape,
                str(binding.name),
                key=None if binding.key is None else str(binding.key),
            )
        )
    return tuple(result)


def _intersect(
    left: ValueDeclaration, right: ValueDeclaration, budget: Budget
) -> ValueDeclaration | None:
    """Conjoin existing declaration facets; do not compare RDF value spaces."""
    budget.consume("work")
    if left.kind == right.kind:
        kind = left.kind
    elif {left.kind, right.kind} == {"object", "iri"}:
        kind = "iri"
    else:
        return None
    if (
        left.datatype is not None
        and right.datatype is not None
        and left.datatype != right.datatype
    ):
        return None
    left_enum = left.constraints.enumeration if left.constraints is not None else None
    right_enum = (
        right.constraints.enumeration if right.constraints is not None else None
    )
    assert left_enum is None or isinstance(left_enum, tuple)
    assert right_enum is None or isinstance(right_enum, tuple)
    enumeration: tuple[URIRef | Literal, ...] | None
    if left_enum is not None and right_enum is not None:
        assert isinstance(left_enum, tuple) and isinstance(right_enum, tuple)
        budget.consume("states", len(right_enum) + len(left_enum))
        keys = set()
        for value in right_enum:
            bound_node(value, budget)
            budget.consume("work")
            keys.add(term_key(value))
        selected = []
        for value in left_enum:
            bound_node(value, budget)
            budget.consume("work")
            if term_key(value) in keys:
                selected.append(value)
        enumeration = tuple(selected)
    else:
        enumeration = left_enum if left_enum is not None else right_enum
    budget.consume("states")
    return ValueDeclaration(
        kind,
        datatype=left.datatype or right.datatype,
        constraints=None
        if enumeration is None
        else ValueConstraints(enumeration=enumeration),
    )


class _Importer:
    """Own adapter mappings over the existing selected typed inspection."""

    def __init__(
        self,
        graph: Graph,
        definitions: dict[Node, Definition],
        budget: Budget,
        issues: IssueCollector,
    ) -> None:
        self.graph = graph
        self.definitions = definitions
        self.budget = budget
        self.issues = issues
        self.covered: Covered = set()
        self.untranslated: list[UntranslatedShapeConstraint] = []

    def unknown(
        self, shape: URIRef | BNode, predicate: URIRef, value: Node, reason: str
    ) -> None:
        """Reserve evidence before retaining an exact unrepresented assertion."""
        for term in (shape, predicate, value):
            bound_node(term, self.budget)
        self.budget.consume("states")
        self.budget.consume("bytes", 64)
        self.issues.append(
            Issue(
                "adapter.shape_constraint_untranslated",
                reason,
                str(shape),
                kind="unverified",
            )
        )
        self.untranslated.append(
            UntranslatedShapeConstraint(shape, predicate, value, reason)
        )

    def active(self, node: URIRef | BNode) -> bool:
        """Respect valid authored deactivation without truthy lexical guesses."""
        values = []
        for value in self.graph.objects(node, SH.deactivated):
            self.budget.consume("work")
            bound_node(value, self.budget)
            self.budget.consume("states")
            values.append(value)
        if not values:
            return True
        if (
            len(values) == 1
            and isinstance(values[0], Literal)
            and literal_datatype(values[0]) == XSD.boolean
            and str(values[0]) in ("true", "false", "1", "0")
        ):
            return str(values[0]) in ("false", "0")
        self.unknown(
            node,
            SH.deactivated,
            values[0],
            "Malformed deactivation cannot qualify an imported shape",
        )
        return False

    def branches(
        self,
        node: URIRef | BNode,
        constraints: tuple[ShaclConstraint, ...],
        active: frozenset[Node] = frozenset(),
        depth: int = 0,
    ) -> tuple[tuple[ValueDeclaration, ...], Covered] | None:
        """Map finite value-local alternatives through existing declaration facets."""
        self.budget.consume("work")
        self.budget.consume("states", 2 + len(active))
        if depth > min(self.budget.limits.max_depth, 64):
            raise LimitExceeded("Imported value alternatives exceed bounded depth")
        if node in active:
            return None
        self.budget.consume("states", 3 * len(constraints))
        result: tuple[ValueDeclaration, ...] = (
            ValueDeclaration("object"),
            ValueDeclaration("literal"),
        )
        covered: Covered = set()
        for constraint in constraints:
            self.budget.consume("work")
            alternatives: tuple[ValueDeclaration, ...]
            if isinstance(constraint, ShaclValue):
                if (
                    constraint.class_iri is not None
                    or constraint.has_value is not ABSENT
                ):
                    return None
                if (
                    constraint.node_kind is not None
                    and constraint.node_kind not in _KIND_BRANCHES
                ):
                    return None
                kinds = _KIND_BRANCHES.get(
                    constraint.node_kind or "", ("object", "literal")
                )
                self.budget.consume("states", len(kinds))
                if constraint.datatype is not None:
                    bound_text(constraint.datatype, self.budget)
                if constraint.in_values is not None:
                    self.budget.consume("states", len(constraint.in_values))
                    for term in constraint.in_values:
                        self.budget.consume("work")
                        bound_node(term, self.budget)
                alternatives = tuple(
                    ValueDeclaration(
                        kind,
                        datatype=constraint.datatype if kind == "literal" else None,
                        constraints=None
                        if constraint.in_values is None
                        else ValueConstraints(enumeration=constraint.in_values),
                    )
                    for kind in kinds
                    if constraint.datatype is None or kind == "literal"
                )
                for predicate, enabled in (
                    (SH.datatype, constraint.datatype is not None),
                    (SH.nodeKind, constraint.node_kind is not None),
                    (SH["in"], constraint.in_values is not None),
                ):
                    if enabled:
                        covered.add((node, predicate))
            elif isinstance(constraint, ShaclLogical) and constraint.operator in (
                "and",
                "or",
            ):
                if not constraint.shapes:
                    return None
                combined: tuple[ValueDeclaration, ...] = (
                    () if constraint.operator == "or" else result
                )
                local: Covered = set()
                for reference in constraint.shapes:
                    self.budget.consume("work")
                    target = self.definitions.get(reference.identifier)
                    if (
                        not isinstance(target, ShaclNodeShape)
                        or target.properties
                        or not self.active(target.identifier)
                        or not self.value_only(target.identifier)
                    ):
                        return None
                    translated = self.branches(
                        target.identifier,
                        target.constraints,
                        active | {node},
                        depth + 1,
                    )
                    if translated is None:
                        return None
                    other, seen = translated
                    self.budget.consume("states", len(seen) + 1)
                    local.update(seen)
                    if constraint.operator == "or":
                        self.budget.consume("states", len(combined) + len(other))
                        combined = (*combined, *other)
                    else:
                        combined = self.conjoin(combined, other)
                alternatives = combined
                covered.update(local)
                covered.add((node, SH[constraint.operator]))
            else:
                return None
            result = self.conjoin(result, alternatives)
            if not result:
                return None
        return result, covered

    def value_only(self, node: URIRef | BNode) -> bool:
        """Reject alternative branches with unrepresented original parameters."""
        for _, predicate, value in self.graph.triples((node, None, None)):
            self.budget.consume("work")
            bound_node(predicate, self.budget)
            bound_node(value, self.budget)
            if (
                predicate not in _ANNOTATIONS
                and predicate
                not in (
                    SH.datatype,
                    SH.nodeKind,
                    SH["in"],
                    SH["and"],
                    SH["or"],
                    SH.deactivated,
                )
                and not (predicate == RDF.type and value == SH.NodeShape)
            ):
                return False
        return True

    def conjoin(
        self, left: tuple[ValueDeclaration, ...], right: tuple[ValueDeclaration, ...]
    ) -> tuple[ValueDeclaration, ...]:
        """Bound alternative products before allocating conjunction results."""
        self.budget.consume("states", len(left) * len(right))
        result = []
        for first in left:
            for second in right:
                merged = _intersect(first, second, self.budget)
                if merged is not None:
                    result.append(merged)
        return tuple(result)

    def field(
        self, definition: ShaclPropertyShape, binding: ShapeFieldBinding
    ) -> FieldDeclaration | None:
        """Import a supported path and independently supported property facets."""
        path = cast(RdfPath, definition.path)
        reverse = (
            path.kind == "inverse"
            and len(path.children) == 1
            and path.children[0].kind == "predicate"
        )
        predicate = path.children[0].node if reverse else path.node
        if not (path.kind == "predicate" or reverse) or not isinstance(
            predicate, URIRef
        ):
            return None
        if predicate == RDF.type:
            return None
        values: tuple[ValueDeclaration, ...] = (
            ValueDeclaration("object"),
            ValueDeclaration("literal"),
        )
        local: Covered = set()
        self.budget.consume("states", 3 * len(definition.constraints) + 1)
        minimum = maximum = None
        for constraint in definition.constraints:
            self.budget.consume("work")
            if isinstance(constraint, ShaclCount):
                for count in (constraint.minimum, constraint.maximum):
                    if count is not None and count.bit_length() > min(
                        4096, self.budget.limits.max_bytes
                    ):
                        raise LimitExceeded(
                            "Imported count exceeds bounded representation"
                        )
                minimum, maximum = constraint.minimum, constraint.maximum
                local.update(
                    (
                        (definition.identifier, SH.minCount),
                        (definition.identifier, SH.maxCount),
                    )
                )
            else:
                translated = self.branches(definition.identifier, (constraint,))
                if translated is not None:
                    other, covered = translated
                    merged = self.conjoin(values, other)
                    if merged:
                        values = merged
                        self.budget.consume("states", len(covered))
                        local.update(covered)
        if minimum is not None and maximum is not None and minimum > maximum:
            return None
        self.budget.consume("states", len(local) + 1)
        self.covered.update(local)
        self.covered.add((definition.identifier, SH.path))
        return FieldDeclaration(
            binding.name,
            str(predicate),
            values,
            key=binding.key,
            reverse=reverse,
            output=OutputRequirement(
                allow_null=True,
                allow_empty=True,
                minimum_values=minimum,
                maximum_values=maximum,
            ),
        )

    def remainder(self, nodes: set[URIRef | BNode]) -> None:
        """Keep each unrepresented original constraint visible without recopying RDF."""
        for node in nodes:
            self.budget.consume("work")
            if not self.active(node):
                continue
            for _, predicate, value in self.graph.triples((node, None, None)):
                self.budget.consume("work")
                bound_node(predicate, self.budget)
                bound_node(value, self.budget)
                if (
                    (node, predicate) in self.covered
                    or predicate in _ANNOTATIONS
                    or predicate == SH.deactivated
                    or predicate == RDF.type
                    and value in (SH.NodeShape, SH.PropertyShape)
                ):
                    continue
                assert isinstance(predicate, URIRef)
                self.unknown(
                    node,
                    predicate,
                    value,
                    "The original constraint remains required outside the "
                    "output-contract subset",
                )


def import_shape_contracts(
    shapes: PreparedSource,
    roots: Sequence[str | URIRef | BNode],
    bindings: Sequence[ShapeFieldBinding],
    *,
    model: str,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShapeContractImport:
    """Import explicitly bound ordinary property fields; retain every limitation.

    This finite subset supports predicate/inverse paths, RDF counts, datatypes,
    representable node kinds, exact named/literal enums and value-local AND/OR.
    It does not infer JSON presence/null rules, list representation, targets or
    exact rdf:type promises from sh:class. Use the extraction report with the
    ordinary serializer/output checker; partial declarations alone are not a
    complete contract. The default extraction report requires aggregate entity
    checking, since a source key is not the whole RDF property. Use
    check_imported_output for that actual same-preparation qualification.
    No optional SHACL execution backend is needed for import.
    """
    if not isinstance(shapes, PreparedSource):
        raise InputError("Shape import requires an owned PreparedSource")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Shape import limits conflict with the operation budget")
    if not isinstance(model, str) or isinstance(model, Node):
        raise InputError("Shape import requires an explicit model identifier")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    inspection: ShapeInspection | None = None
    importer: _Importer | None = None
    fields: list[FieldDeclaration] = []
    declarations: tuple[ModelDeclaration, ...] = ()
    children: tuple[Report, ...] = ()
    checked = 0
    requested = 1
    try:
        bound_text(model, operation)
        if not model.strip():
            raise InputError("Shape import requires an explicit model identifier")
        model = str(model)
        captured = _capture(bindings, operation)
        requested = max(1, len(captured))
        inspection = inspect_shapes(
            shapes, roots, graph_name=graph_name, budget=operation
        )
        retain_report(inspection.report, operation, issues)
        children = (inspection.report,)
        graph = shapes._read(graph_name, budget=operation)
        operation.consume("states", len(inspection.definitions))
        definitions: dict[Node, Definition] = {
            definition.identifier: definition for definition in inspection.definitions
        }
        importer = _Importer(graph, definitions, operation, issues)
        operation.consume("states", len(inspection.profile.roots))
        eligible: set[Node] = set(inspection.profile.roots)
        for root in inspection.profile.roots:
            definition = definitions.get(root)
            if isinstance(definition, ShaclNodeShape) and importer.active(root):
                operation.consume("states", 2 * len(definition.properties))
                for reference in definition.properties:
                    operation.consume("work")
                    eligible.add(reference.identifier)
                importer.covered.add((root, SH.property))
        names: set[str] = set()
        keys: set[str] = set()
        bound: set[Node] = set()
        operation.consume("states", 3 * len(captured))
        for binding in captured:
            operation.consume("work")
            key = binding.key or binding.name
            if binding.name in names or key in keys or binding.shape in bound:
                raise InputError(
                    "Shape bindings must have unique members, keys and shapes"
                )
            names.add(binding.name)
            keys.add(key)
            definition = definitions.get(binding.shape)
            if (
                not isinstance(definition, ShaclPropertyShape)
                or binding.shape not in eligible
                or isinstance(binding.shape, BNode)
                and definition.identifier is not binding.shape
            ):
                issues.append(
                    Issue(
                        "adapter.shape_binding_unverified",
                        "The bound property shape is not a selected owned "
                        "field definition",
                        kind="unverified",
                    )
                )
                continue
            bound.add(binding.shape)
            checked += 1
            if not importer.active(definition.identifier):
                continue
            operation.consume("states")
            field = importer.field(definition, binding)
            if field is not None:
                fields.append(field)
        for definition in inspection.definitions:
            operation.consume("work")
            if (
                isinstance(definition, ShaclPropertyShape)
                and definition.identifier not in bound
                and importer.active(definition.identifier)
            ):
                issues.append(
                    Issue(
                        "adapter.shape_binding_unverified",
                        "A selected property shape has no serializer member binding",
                        str(definition.identifier),
                        kind="unverified",
                    )
                )
        operation.consume(
            "states",
            len(inspection.profile.roots) + 2 * len(inspection.profile.dependencies),
        )
        nodes = set(inspection.profile.roots)
        for dependency in inspection.profile.dependencies:
            operation.consume("work")
            nodes.update((dependency.shape, dependency.dependency))
        importer.remainder(nodes)
        if not fields:
            issues.append(
                Issue(
                    "adapter.shape_binding_unverified",
                    "No active supported bound property field was imported",
                    kind="unverified",
                )
            )
        operation.consume("states", len(fields) + 1)
        declarations = (ModelDeclaration(model, (), tuple(fields)),)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = Report(
        "shacl-output-contract-import",
        checked,
        tuple(issues),
        requested=requested,
        children=children,
        complete=not any(issue.kind == "exhausted" for issue in issues),
        regime="finite-shacl-output-contract-import",
    )
    source_issues = IssueCollector(operation)
    representation_issues = IssueCollector(operation)
    try:
        operation.consume("states", 4 + 2 * len(report.issues))
        representation_issues.append(
            Issue(
                "adapter.shape_representation_unverified",
                "RDF list heads are not ordinary field values; selected actual "
                "no-list representation remains required",
                kind="unverified",
            )
        )
        source_issues.append(
            Issue(
                "adapter.shape_source_scope_unverified",
                "Selected serializer keys do not establish the complete RDF "
                "predicate values; aggregate entity checking remains required",
                kind="unverified",
            )
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
        report = Report(
            report.scope,
            report.checked,
            tuple(issues),
            requested=report.requested,
            complete=False,
            children=children,
            regime=report.regime,
        )
    rdf_report = Report(
        "shacl-rdf-contract-import",
        checked,
        tuple(representation_issues),
        children=(report,),
        requested=requested,
        regime="rdf-fields-with-required-representation-scope",
    )
    source_report = Report(
        "shacl-source-contract-import",
        checked,
        tuple(source_issues),
        children=(rdf_report,),
        requested=requested,
        regime="source-fields-with-required-rdf-scope",
    )
    return ShapeContractImport._from_import(
        inspection,
        ModelExtraction(declarations, source_report),
        () if importer is None else tuple(importer.untranslated),
        rdf_report,
        report,
    )
