"""Faithful finite Core projections, with unsupported obligations kept required."""

from collections.abc import Sequence
from typing import TypeVar

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.paths import Path

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.application_values import retain_enumeration_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.terms import bound_node, bound_text, literal_term
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)

from .types import ModelShapeBinding, ModelShapeTranslation

_T = TypeVar("_T")


def _size(values: Sequence[_T]) -> int:
    """Read a finite inventory count once, before its bounded capture."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Model shape inputs require finite typed sequences")
    return len(values)


def _capture(
    values: Sequence[_T], kind: type[_T], budget: Budget, count: int
) -> tuple[_T, ...]:
    """Capture the cached finite prefix, charging before each caller access."""
    if count > budget.limits.max_states:
        raise LimitExceeded("Model shape inventory exceeds max_states")
    result = []
    for index in range(count):
        budget.consume("work")
        budget.consume("states")
        value = values[index]
        if not isinstance(value, kind):
            raise InputError("Model shape inventory contains an invalid declaration")
        result.append(value)
    return tuple(result)


class _Translation:
    """Own operation-local definitions, dependencies and required evidence."""

    def __init__(self, budget: Budget) -> None:
        """Start one bounded translation without retaining cross-operation state."""
        self.budget = budget
        self.issues = IssueCollector(budget)
        self.definitions: list[ShaclNodeShape | ShaclPropertyShape] = []
        self.selection_reports: dict[tuple[int, bool], Report] = {}
        self.bindings: dict[str, ModelShapeBinding] = {}
        self.models: dict[str, ModelDeclaration] = {}
        self.recursive: set[str] = set()

    def unknown(self, location: str, reason: str) -> None:
        """Keep an explicit untranslatable obligation in the required report."""
        self.issues.append(
            Issue("shacl.model_shape_unverified", reason, location, kind="unverified")
        )

    def location(self, *parts: str) -> str:
        """Bound metadata before joining it into a diagnostic anchor."""
        self.budget.consume("work", len(parts) + 1)
        characters = sum(len(part) for part in parts) + max(0, len(parts) - 1)
        if characters > self.budget.limits.max_bytes:
            raise LimitExceeded("Model shape location exceeds max_bytes")
        size = max(0, len(parts) - 1)
        for part in parts:
            self.budget.consume("work", len(part) + 1)
            try:
                size += len(part.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise InputError(
                    "Model shape location requires valid Unicode"
                ) from error
            if size > self.budget.limits.max_bytes:
                raise LimitExceeded("Model shape location exceeds max_bytes")
        self.budget.consume("bytes", size)
        return "/".join(parts)

    def identifier(self) -> BNode:
        """Reserve a generated shape identity before allocating it."""
        self.budget.consume("states")
        self.budget.consume("bytes", 64)
        return BNode()

    def retain(self, shape: ShaclNodeShape | ShaclPropertyShape) -> ShaclShapeRef:
        """Retain one reusable definition after reserving its storage."""
        self.budget.consume("states")
        self.definitions.append(shape)
        return ShaclShapeRef(shape.identifier)

    def iri(self, value: str) -> URIRef:
        """Own an exact validated IRI only after bounding its spelling."""
        bound_text(value, self.budget)
        absolute_iri(value)
        return URIRef(value)

    def types(self, values: tuple[str, ...]) -> tuple[ShaclShapeRef, ...]:
        """Require each explicit type assertion without subclass entailment."""
        constraints = []
        for value in values:
            self.budget.consume("work")
            self.budget.consume("states")
            constraints.append(ShaclValue(has_value=self.iri(value)))
        if not constraints:
            return ()
        return (
            self.retain(
                ShaclPropertyShape(self.identifier(), RDF.type, tuple(constraints))
            ),
        )

    def dependencies(self) -> None:
        """Find cyclic model dependencies iteratively without expanding references."""
        pending: dict[str, int] = {}
        parents: dict[str, list[str]] = {}
        for name, model in self.models.items():
            dependencies = set()
            for field in model.fields:
                self.budget.consume("work")
                for value in field.values:
                    self.budget.consume("work")
                    if value.model is not None:
                        bound_text(value.model, self.budget)
                        child = str(value.model)
                        if child not in self.models:
                            raise InputError(
                                "A referenced child model needs an explicit "
                                "declaration and binding"
                            )
                        self.budget.consume("states")
                        dependencies.add(child)
            self.budget.consume("states", len(dependencies) + 1)
            pending[name] = len(dependencies)
            for child in dependencies:
                parents.setdefault(child, []).append(name)
        ready = [name for name, count in pending.items() if not count]
        while ready:
            self.budget.consume("work")
            for name in parents.get(ready.pop(), ()):
                self.budget.consume("work")
                pending[name] -= 1
                if not pending[name]:
                    ready.append(name)
        self.recursive = {name for name, count in pending.items() if count}

    def enumeration(
        self, value: ValueDeclaration, location: str
    ) -> tuple[URIRef | Literal, ...] | None:
        """Copy only a qualified closed allowed set; preserve selection evidence
        once.
        """
        constraints = value.constraints
        if constraints is None or constraints.enumeration is None:
            return None
        enumeration = constraints.enumeration
        if isinstance(enumeration, ValueEnumeration):
            retain_enumeration_report(
                constraints,
                self.selection_reports,
                budget=self.budget,
                issues=self.issues,
            )
            if not enumeration.qualified:
                self.unknown(
                    location,
                    "Open or incomplete allowed-set selection cannot become a "
                    "closed SHACL enumeration",
                )
                return None
            terms = enumeration.values
        else:
            terms = enumeration
        result: list[URIRef | Literal] = []
        for term in terms:
            self.budget.consume("work")
            self.budget.consume("states")
            bound_node(term, self.budget)
            result.append(
                literal_term(str(term), datatype=term.datatype, language=term.language)
                if isinstance(term, Literal)
                else URIRef(term)
            )
        return tuple(result)

    def branch(
        self, value: ValueDeclaration, location: str, *, child_safe: bool
    ) -> ShaclShapeRef:
        """Translate one conjoined alternative without weakening unsupported facets."""
        rules: list[ShaclConstraint] = []
        properties = self.types(value.types)
        kinds = {"literal": "literal", "iri": "iri", "object": "blank_or_iri"}
        if value.kind in kinds:
            rules.append(ShaclValue(node_kind=kinds[value.kind]))  # type: ignore[arg-type]
        else:
            self.unknown(
                location, "Unknown RDF value kind has no complete Core projection"
            )
        enumeration = self.enumeration(value, location)
        if enumeration is not None:
            rules.append(ShaclValue(in_values=enumeration))
        if value.datatype is not None:
            datatype = str(self.iri(value.datatype))
            rules.append(ShaclValue(datatype=datatype))
        else:
            datatype = None
        if value.kind == "literal":
            finite_valid = enumeration is not None
            if enumeration is not None:
                for term in enumeration:
                    self.budget.consume("work")
                    if (
                        isinstance(term, Literal)
                        and not check_literal(term, budget=self.budget).verified
                    ):
                        finite_valid = False
            if (
                datatype not in (str(XSD.string), str(RDF.langString))
                and not finite_valid
            ):
                self.unknown(
                    location,
                    "Native literal lexical validation is not fully represented by "
                    "this SHACL datatype rule",
                )
        if (
            value.language is not None
            or value.direction is not None
            or value.container is not None
            or value.role is not None
        ):
            self.unknown(
                location,
                "Language, direction, container or resource-role obligations "
                "require the original model checks",
            )
        constraints = value.constraints
        if constraints is not None:
            for text in (constraints.pattern, *constraints.iri_namespaces):
                if text is not None:
                    bound_text(text, self.budget)
            if (
                any(
                    item is not None
                    for item in (
                        constraints.minimum,
                        constraints.maximum,
                        constraints.min_length,
                        constraints.max_length,
                        constraints.pattern,
                    )
                )
                or constraints.iri_namespaces
            ):
                self.unknown(
                    location,
                    "Numeric, length, pattern or namespace predicates are outside "
                    "this Core projection",
                )
        if value.model is not None:
            child = str(value.model)
            if not child_safe or child in self.recursive:
                self.unknown(
                    location,
                    "Ambiguous or recursive child-model obligations require native "
                    "model binding",
                )
            else:
                rules.append(ShaclShapeRef(self.bindings[child].shape))
        return self.retain(ShaclNodeShape(self.identifier(), properties, tuple(rules)))

    def field(
        self, field: FieldDeclaration, model: ModelDeclaration
    ) -> ShaclShapeRef | None:
        """Project a simple explicit predicate, direction, alternatives and RDF
        counts.
        """
        location = self.location(model.identifier, field.name)
        for text in (field.location, field.key or ""):
            bound_text(text, self.budget)
        if field.predicate == "@id" or field.reverse and field.predicate == "@type":
            self.unknown(
                location,
                "The field requires source or subject-identity semantics outside a "
                "simple property path",
            )
            return None
        predicate = (
            RDF.type if field.predicate == "@type" else self.iri(field.predicate)
        )
        path: URIRef | Path = ~predicate if field.reverse else predicate
        rules: list[ShaclConstraint] = []
        minimum = 1 if field.emission == "guaranteed" else None
        maximum = None
        if field.output is not None:
            output = field.output
            if output.minimum_values is not None:
                minimum = max(minimum or 0, output.minimum_values)
            maximum = output.maximum_values
            if (
                output.require_property
                or not output.allow_null
                or not output.allow_empty
            ):
                self.unknown(
                    location,
                    "Source property presence, null and empty permissions cannot be "
                    "established from RDF shapes",
                )
            if (
                output.minimum_list_members is not None
                or output.maximum_list_members is not None
            ):
                self.unknown(
                    location,
                    "Per-list member counts require the original list-aware output "
                    "checks",
                )
        if minimum is not None or maximum is not None:
            if minimum is not None and maximum is not None and minimum > maximum:
                raise InputError(
                    "Guaranteed emission conflicts with the explicit maximum value "
                    "count"
                )
            rules.append(ShaclCount(minimum, maximum))
        alternatives = []
        for value in field.values:
            self.budget.consume("work")
            self.budget.consume("states")
            alternatives.append(
                self.branch(value, location, child_safe=len(field.values) == 1)
            )
        if alternatives:
            rules.append(
                alternatives[0]
                if len(alternatives) == 1
                else ShaclLogical("or", tuple(alternatives))
            )
        else:
            self.unknown(
                location,
                "An empty value-branch inventory has no supported positive projection",
            )
        return self.retain(ShaclPropertyShape(self.identifier(), path, tuple(rules)))

    def model(self, model: ModelDeclaration) -> None:
        """Keep simultaneous asserted types, identity and each explicit field
        independent.
        """
        binding = self.bindings[str(model.identifier)]
        properties = list(self.types(model.types))
        occurrence_required = bool(model.types)
        for field in model.fields:
            self.budget.consume("work")
            self.budget.consume("states")
            projected = self.field(field, model)
            if projected is not None:
                properties.append(projected)
                occurrence_required = occurrence_required or (
                    field.emission == "guaranteed"
                    or field.output is not None
                    and (field.output.minimum_values or 0) > 0
                )
        if binding.focus_nodes and not occurrence_required:
            self.unknown(
                model.identifier,
                "Targeted subject occurrence requires native source qualification; "
                "the translated optional rules do not require an RDF assertion",
            )
        rules: list[ShaclConstraint] = []
        if model.subject.expected_iri is not None:
            rules.append(ShaclValue(has_value=self.iri(model.subject.expected_iri)))
        if not model.subject.allow_blank:
            rules.append(ShaclValue(node_kind="iri"))
        if model.emission_groups or model.coverage is not None:
            self.unknown(
                model.identifier,
                "Conditional emission groups and closed source coverage require the "
                "original model checks",
            )
        self.retain(
            ShaclNodeShape(
                binding.shape,
                tuple(properties),
                tuple(rules),
                target_nodes=binding.focus_nodes,
            )
        )


def compile_model_shapes(
    models: Sequence[ModelDeclaration],
    bindings: Sequence[ModelShapeBinding],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ModelShapeTranslation:
    """Compile explicit asserted-RDF constraints and retain every unsupported scope.

    Targets are supplied IRIs, never inferred from classes or Python optionality.
    Numeric datatype rules preserve exact datatype identity, but unbounded native
    lexical checks remain required because maintained SHACL accepts a wider set
    of spellings. Closed native-valid literal enums can qualify that finite scope.
    Source presence/null/empty, list, conditional and other unrepresented rules
    keep the report incomplete even when a useful partial source is returned.
    """
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Model shape compilation requires Limits and Budget objects")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Model shape limits must match the supplied operation budget")
    operation = budget if budget is not None else Budget(limits)
    state = _Translation(operation)
    checked = 0
    requested = 1
    source = None
    try:
        model_count = _size(models)
        requested = max(1, model_count)
        captured_models = _capture(models, ModelDeclaration, operation, model_count)
        captured_bindings = _capture(
            bindings, ModelShapeBinding, operation, _size(bindings)
        )
        if not captured_models or not captured_bindings:
            raise InputError(
                "Model shape compilation requires nonempty models and explicit bindings"
            )
        shapes = set()
        for model in captured_models:
            bound_text(model.identifier, operation)
            bound_text(model.location, operation)
            name = str(model.identifier)
            if name in state.models:
                raise InputError("Model shape declarations require unique identifiers")
            state.models[name] = model
        for binding in captured_bindings:
            bound_text(binding.model, operation)
            bound_text(binding.location, operation)
            bound_node(binding.shape, operation)
            for node in binding.focus_nodes:
                operation.consume("work")
                bound_node(node, operation)
            if (
                binding.model not in state.models
                or binding.model in state.bindings
                or binding.shape in shapes
            ):
                raise InputError(
                    "Bindings require distinct known models and distinct shape IRIs"
                )
            state.bindings[binding.model] = binding
            shapes.add(binding.shape)
        if state.models.keys() != state.bindings.keys() or not any(
            binding.focus_nodes for binding in captured_bindings
        ):
            raise InputError(
                "Every supplied model needs one binding and the selected scope "
                "needs an explicit target"
            )
        state.dependencies()
        for model in captured_models:
            operation.consume("work")
            state.model(model)
            checked += 1
        source = compile_shapes(tuple(state.definitions), budget=operation)
    except LimitExceeded as error:
        state.issues.exhausted(str(error))
    report = Report(
        "shacl.model_shapes",
        checked,
        tuple(state.issues),
        requested=max(requested, checked),
        complete=source is not None and not state.issues,
        children=tuple(state.selection_reports.values()),
    )
    return ModelShapeTranslation(tuple(state.definitions), source, report)
