"""Compile explicit finite SHACL authoring into an owned ordinary RDF artifact."""

from collections.abc import Sequence

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.absence import ABSENT
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.paths import RdfPath, capture_path
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.shacl.authoring.types import (
    ShaclClosed,
    ShaclComparison,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclQualifiedCount,
    ShaclShapeRef,
    ShaclValue,
)

_SINGLE = frozenset(
    (
        SH.datatype,
        SH.closed,
        SH.ignoredProperties,
        SH.nodeKind,
        SH.minCount,
        SH.maxCount,
        SH["in"],
        SH.qualifiedValueShape,
        SH.qualifiedMinCount,
        SH.qualifiedMaxCount,
        SH.qualifiedValueShapesDisjoint,
    )
)
_NODE_KINDS = {
    "iri": SH.IRI,
    "blank": SH.BlankNode,
    "literal": SH.Literal,
    "blank_or_iri": SH.BlankNodeOrIRI,
    "blank_or_literal": SH.BlankNodeOrLiteral,
    "iri_or_literal": SH.IRIOrLiteral,
}
_COMPARISONS = {
    "equals": SH.equals,
    "disjoint": SH.disjoint,
    "less_than": SH.lessThan,
    "less_than_or_equals": SH.lessThanOrEquals,
}
_PATHS = {
    "inverse": SH.inversePath,
    "alternative": SH.alternativePath,
    "zero_or_more": SH.zeroOrMorePath,
    "one_or_more": SH.oneOrMorePath,
    "zero_or_one": SH.zeroOrOnePath,
}


class _Compiler:
    """Retain one bounded operation graph and its explicitly supplied definitions."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.graph = Graph()
        self.definitions: dict[URIRef | BNode, ShaclNodeShape | ShaclPropertyShape] = {}

    def text(self, value: object) -> str:
        """Charge metadata before retaining or interpreting its original spelling."""
        if not isinstance(value, str):
            raise InputError("SHACL metadata must be text")
        self.budget.consume("work", len(value) + 1)
        if len(value) > self.budget.limits.max_bytes:
            raise LimitExceeded("SHACL metadata exceeds max_bytes")
        try:
            raw = value.encode("utf-8")
        except UnicodeError as error:
            raise InputError("SHACL metadata must be valid Unicode") from error
        if len(raw) > self.budget.limits.max_bytes:
            raise LimitExceeded("SHACL metadata exceeds max_bytes")
        self.budget.consume("bytes", len(raw))
        return str(value)

    def iri(self, value: object) -> URIRef:
        """Own an exact absolute named IRI, rejecting other RDF string subclasses."""
        if not isinstance(value, str) or isinstance(value, BNode | Literal):
            raise InputError("SHACL predicates and class/datatype values require IRIs")
        text = self.text(value)
        try:
            absolute_iri(text)
        except InputError as error:
            raise InputError("SHACL IRI must be absolute and well formed") from error
        return URIRef(text)

    def identifier(self, value: object) -> URIRef | BNode:
        """Own the exact explicit identity of a shape definition or reference."""
        if isinstance(value, URIRef):
            return self.iri(value)
        if isinstance(value, BNode):
            return BNode(self.text(value))
        raise InputError("Shape identifiers require RDF IRI or blank nodes")

    def term(self, value: object) -> URIRef | Literal:
        """Own a named or literal constant without datatype lexical normalization."""
        if isinstance(value, URIRef):
            return self.iri(value)
        if not isinstance(value, Literal):
            raise InputError("SHACL constants require named IRIs or RDF literals")
        text = self.text(value)
        datatype = None if value.datatype is None else str(self.iri(value.datatype))
        language = None if value.language is None else self.text(value.language)
        return literal_term(text, datatype=datatype, language=language)

    def items(self, values: object) -> tuple[object, ...]:
        """Capture immutable record inventories with charged bounded indexing."""
        if type(values) is not tuple:
            raise InputError("SHACL record inventories require immutable tuples")
        if len(values) > self.budget.limits.max_states:
            raise LimitExceeded("SHACL inventory exceeds max_states")
        result: list[object] = []
        for index in range(len(values)):
            self.budget.consume("work")
            self.budget.consume("states")
            result.append(values[index])
        return tuple(result)

    def add(self, subject: Node, predicate: URIRef, value: Node) -> None:
        """Bound every produced assertion and reject conflicting singleton values."""
        self.budget.consume("work")
        self.budget.consume("states")
        if predicate in _SINGLE and any(
            old != value for old in self.graph.objects(subject, predicate)
        ):
            raise InputError("Conflicting single-valued SHACL parameters")
        self.budget.consume("triples")
        self.graph.add((subject, predicate, value))

    def rdf_list(self, values: tuple[Node, ...]) -> Node:
        """Emit an owned finite RDF collection, including the explicit empty list."""
        tail: Node = RDF.nil
        for value in reversed(values):
            head = BNode()
            self.add(head, RDF.first, value)
            self.add(head, RDF.rest, tail)
            tail = head
        return tail

    def reference(self, value: object, *, kind: type | None = None) -> URIRef | BNode:
        """Resolve only supplied shape identities, retaining recursive links as RDF."""
        if type(value) is not ShaclShapeRef:
            raise InputError("Expected an explicit SHACL shape reference")
        node = self.identifier(value.identifier)
        definition = self.definitions.get(node)
        if definition is None:
            raise InputError("SHACL shape reference has no supplied definition")
        if kind is not None and type(definition) is not kind:
            raise InputError("SHACL reference has the wrong shape kind")
        return node

    def path(self, value: RdfPath) -> Node:
        """Serialize the shared captured path representation into SHACL RDF."""
        self.budget.consume("work")
        self.budget.consume("states")
        if value.kind == "predicate":
            return value.node
        children = tuple(self.path(child) for child in value.children)
        if value.kind == "sequence":
            return self.rdf_list(children)
        node = BNode()
        argument = (
            self.rdf_list(children) if value.kind == "alternative" else children[0]
        )
        self.add(node, _PATHS[value.kind], argument)
        return node

    def counts(
        self,
        node: Node,
        minimum: int | None,
        maximum: int | None,
        *,
        qualified: bool = False,
    ) -> None:
        """Compile explicit nonnegative count bounds without inferring obligations."""
        if minimum is None and maximum is None:
            raise InputError("A count constraint requires a minimum or maximum")
        for value in (minimum, maximum):
            if value is not None and (type(value) is not int or value < 0):
                raise InputError("SHACL counts require nonnegative integers")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise InputError("SHACL minimum count exceeds maximum")
        for value, predicate in (
            (minimum, SH.qualifiedMinCount if qualified else SH.minCount),
            (maximum, SH.qualifiedMaxCount if qualified else SH.maxCount),
        ):
            if value is not None:
                # Bound the integer's representation before converting it to text.
                if value.bit_length() > self.budget.limits.max_bytes:
                    raise LimitExceeded("SHACL count representation exceeds max_bytes")
                try:
                    text = str(value)
                except ValueError as error:
                    raise LimitExceeded(
                        "SHACL count exceeds supported integer representation"
                    ) from error
                term = literal_term(self.text(text), datatype=str(XSD.integer))
                self.add(node, predicate, term)

    def constraint(self, node: Node, value: object, *, property_shape: bool) -> None:
        """Emit one supported Core record; unsupported constructs fail closed."""
        self.budget.consume("work")
        self.budget.consume("states")
        if type(value) is ShaclShapeRef:
            self.add(node, SH.node, self.reference(value, kind=ShaclNodeShape))
        elif type(value) is ShaclClosed:
            if type(value.enabled) is not bool:
                raise InputError("SHACL closed policy requires an explicit boolean")
            self.add(node, SH.closed, Literal(value.enabled))
            if value.ignored_properties:
                ignored = tuple(
                    self.iri(item) for item in self.items(value.ignored_properties)
                )
                self.add(node, SH.ignoredProperties, self.rdf_list(ignored))
        elif type(value) is ShaclValue:
            if value.datatype is not None:
                self.add(node, SH.datatype, self.iri(value.datatype))
            if value.class_iri is not None:
                self.add(node, SH["class"], self.iri(value.class_iri))
            if value.node_kind is not None:
                kind = self.text(value.node_kind)
                if kind not in _NODE_KINDS:
                    raise InputError("Unsupported SHACL node kind")
                self.add(node, SH.nodeKind, _NODE_KINDS[kind])
            if value.has_value is not ABSENT:
                self.add(node, SH.hasValue, self.term(value.has_value))
            if value.in_values is not None:
                terms = tuple(self.term(item) for item in self.items(value.in_values))
                self.add(node, SH["in"], self.rdf_list(terms))
        elif type(value) is ShaclLogical:
            operator = self.text(value.operator)
            if operator not in ("and", "or", "xone", "not"):
                raise InputError("Unsupported SHACL logical operator")
            refs = tuple(self.reference(item) for item in self.items(value.shapes))
            if not refs or (operator == "not" and len(refs) != 1):
                raise InputError("Logical constraints require valid reference arity")
            self.add(
                node,
                SH[operator],
                refs[0] if operator == "not" else self.rdf_list(refs),
            )
        elif type(value) in (ShaclCount, ShaclComparison, ShaclQualifiedCount):
            if not property_shape:
                raise InputError("This constraint requires a property shape")
            if type(value) is ShaclCount:
                self.counts(node, value.minimum, value.maximum)
            elif type(value) is ShaclComparison:
                operator = self.text(value.operator)
                if operator not in _COMPARISONS:
                    raise InputError("Unsupported SHACL property comparison")
                self.add(node, _COMPARISONS[operator], self.iri(value.other))
            elif type(value) is ShaclQualifiedCount:
                if type(value.disjoint) is not bool:
                    raise InputError("Qualified count disjoint policy must be boolean")
                self.add(node, SH.qualifiedValueShape, self.reference(value.shape))
                self.counts(node, value.minimum, value.maximum, qualified=True)
                self.add(node, SH.qualifiedValueShapesDisjoint, Literal(value.disjoint))
        else:
            raise InputError("Unsupported SHACL authoring constraint")

    def compile(self, value: ShaclNodeShape | ShaclPropertyShape) -> None:
        """Emit one named definition without recursively expanding its references."""
        node = self.identifier(value.identifier)
        if type(value) is ShaclNodeShape:
            self.add(node, RDF.type, SH.NodeShape)
            for iri in self.items(value.target_classes):
                self.add(node, SH.targetClass, self.iri(iri))
            for term in self.items(value.target_nodes):
                self.add(node, SH.targetNode, self.term(term))
            for item in self.items(value.properties):
                self.add(
                    node, SH.property, self.reference(item, kind=ShaclPropertyShape)
                )
            property_shape = False
        elif type(value) is ShaclPropertyShape:
            self.add(node, RDF.type, SH.PropertyShape)
            captured = capture_path(value.path, budget=self.budget)
            self.add(node, SH.path, self.path(captured))
            property_shape = True
        for item in self.items(value.constraints):
            self.constraint(node, item, property_shape=property_shape)


def compile_shapes(
    shapes: Sequence[ShaclNodeShape | ShaclPropertyShape],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparedSource:
    """Compile caller-authored Core records into an owned RDF shapes graph.

    Construction performs no data validation, ontology inference or universal
    recursion qualification. Use ``validate_shapes(data, result, preflight=True)``
    for maintained meta-SHACL and actual data checks. Every definition/reference
    must be supplied; failure returns no partial artifact. Recursive links remain
    references and their validation semantics belong to the selected processor.
    """
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Compiler limits must match the supplied operation budget")
    operation = budget if budget is not None else Budget(limits)
    if not isinstance(shapes, Sequence):
        raise InputError("Shapes require a finite definition sequence")
    count = len(shapes)
    if count > operation.limits.max_states:
        raise LimitExceeded("Shape definition count exceeds max_states")
    compiler = _Compiler(operation)
    selected: list[ShaclNodeShape | ShaclPropertyShape] = []
    for index in range(count):
        operation.consume("work")
        operation.consume("states")
        item = shapes[index]
        if type(item) not in (ShaclNodeShape, ShaclPropertyShape):
            raise InputError("Expected a supported typed SHACL shape definition")
        selected.append(item)
        node = compiler.identifier(item.identifier)
        if node in compiler.definitions:
            raise InputError("Duplicate SHACL shape identifiers")
        compiler.definitions[node] = item
    if not selected:
        raise InputError("At least one SHACL shape definition is required")
    for item in selected:
        compiler.compile(item)
    return prepare_rdf(compiler.graph, budget=operation)
