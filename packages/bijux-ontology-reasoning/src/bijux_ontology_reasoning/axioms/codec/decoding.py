"""Bounded decoding of the finite maintained-object projection protocol."""

from __future__ import annotations

from typing import Any, Literal, cast

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri

from ...backend.owlapi.protocol import result_counter, result_text
from .. import types as t


class ProjectionDecoder:
    """Own immutable records only after qualifying each node and its native role."""

    def __init__(self, budget: Budget) -> None:
        """Retain the operation allowance without creating a result cache."""
        self.budget = budget

    def text(self, value: object) -> str:
        """Bound native text before comparison, hashing or retained allocation."""
        return cast(str, result_text(value, self.budget))

    def iri(self, value: object) -> str:
        """Retain only an actual absolute named IRI."""
        return str(absolute_iri(self.text(value)))

    def records(self, value: object) -> list[Any]:
        """Qualify an actual finite result array before visiting its elements."""
        if not isinstance(value, list) or len(value) > self.budget.limits.max_states:
            raise ResourceError("Invalid OWL projection inventory")
        self.budget.consume("states", len(value))
        self.budget.consume("work", len(value) + 1)
        return value

    def node(self, value: object, depth: int) -> tuple[str, dict[str, Any]]:
        """Reserve a typed node before decoding any nested structure."""
        if depth > self.budget.limits.max_depth:
            raise LimitExceeded("OWL projection exceeds max_depth")
        self.budget.consume("states")
        item = object_value(value, location="OWL projection")
        return self.text(item.get("kind")), item

    def boolean(self, value: object) -> bool:
        """Reject integral substitutes for maintained boolean flags."""
        if type(value) is not bool:
            raise ResourceError("Invalid OWL projection boolean")
        self.budget.consume("work")
        return value

    def entity(
        self, value: object, depth: int, roles: tuple[str, ...]
    ) -> t.NamedEntity:
        """Keep entity role distinctions instead of interpreting an IRI as a type."""
        kind, item = self.node(value, depth)
        role = self.text(item.get("role"))
        if kind != "entity" or role not in roles:
            raise ResourceError("OWL entity has an unexpected role")
        return t.NamedEntity(cast(t.EntityKind, role), self.iri(item.get("iri")))

    def property(self, value: object, depth: int) -> t.PropertyExpression:
        """Decode only the maintained object/data property expression families."""
        kind, item = self.node(value, depth)
        iri = self.iri(item.get("iri"))
        if kind == "object_property":
            return t.ObjectProperty(iri, self.boolean(item.get("inverse")))
        if kind == "data_property":
            return t.DataProperty(iri)
        raise ResourceError("Unsupported OWL property expression")

    def literal(self, value: object, depth: int) -> t.NativeLiteral:
        """Preserve native parsed literal spelling and language metadata."""
        kind, item = self.node(value, depth)
        if kind != "literal":
            raise ResourceError("OWL data value is not a native literal")
        language = result_text(item.get("language"), self.budget, optional=True)
        return t.NativeLiteral(
            self.text(item.get("lexical")), self.iri(item.get("datatype")), language
        )

    def annotation_value(
        self, value: object, depth: int
    ) -> t.IriValue | t.NativeLiteral:
        """Anonymous annotation values require unavailable correspondence."""
        item = object_value(value, location="OWL annotation value")
        kind = self.text(item.get("kind"))
        if kind == "literal":
            return self.literal(value, depth)
        if kind == "iri":
            self.node(value, depth)
            return t.IriValue(self.iri(item.get("iri")))
        raise ResourceError("Unsupported OWL annotation value")

    def annotation(self, value: object, depth: int) -> t.OwlAnnotation:
        """Retain recursively nested axiom/ontology annotations."""
        kind, item = self.node(value, depth)
        if kind != "annotation":
            raise ResourceError("Invalid OWL annotation projection")
        return t.OwlAnnotation(
            self.iri(item.get("property")),
            self.annotation_value(item.get("value"), depth + 1),
            self.annotations(item.get("annotations"), depth + 1),
        )

    def annotations(self, value: object, depth: int) -> tuple[t.OwlAnnotation, ...]:
        """Own the actual bounded annotation inventory."""
        return tuple(self.annotation(raw, depth) for raw in self.records(value))

    def expression(
        self, value: object, depth: int, *, data: bool = False
    ) -> t.ClassExpression | t.DataRange:
        """Decode supported structural class/data expressions without evaluation."""
        kind, item = self.node(value, depth)
        if kind == "entity":
            role = "datatype" if data else "class"
            if self.text(item.get("role")) != role:
                raise ResourceError("OWL expression has an unexpected entity role")
            return t.NamedEntity(cast(t.EntityKind, role), self.iri(item.get("iri")))
        if kind == ("data_junction" if data else "object_junction"):
            op = self.text(item.get("operator"))
            if op not in ("intersection", "union"):
                raise ResourceError("Invalid OWL Boolean operator")
            operands = self.records(item.get("operands"))
            if data:
                return t.DataJunction(
                    cast(Literal["intersection", "union"], op),
                    tuple(self.data_range(raw, depth + 1) for raw in operands),
                )
            return t.ClassJunction(
                cast(Literal["intersection", "union"], op),
                tuple(self.class_expression(raw, depth + 1) for raw in operands),
            )
        if kind == ("data_complement" if data else "object_complement"):
            return (
                t.DataComplement(self.data_range(item.get("operand"), depth + 1))
                if data
                else t.ClassComplement(
                    self.class_expression(item.get("operand"), depth + 1)
                )
            )
        if data:
            if kind == "data_enumeration":
                return t.DataEnumeration(
                    tuple(
                        self.literal(raw, depth + 1)
                        for raw in self.records(item.get("values"))
                    )
                )
            if kind == "datatype_restriction":
                facets = []
                for raw in self.records(item.get("facets")):
                    facet_kind, facet = self.node(raw, depth + 1)
                    if facet_kind != "facet":
                        raise ResourceError("Invalid OWL datatype facet")
                    facets.append(
                        t.DatatypeFacet(
                            self.iri(facet.get("iri")),
                            self.literal(facet.get("value"), depth + 2),
                        )
                    )
                return t.DatatypeRestriction(
                    self.entity(item.get("datatype"), depth + 1, ("datatype",)),
                    tuple(facets),
                )
            raise ResourceError("Unsupported OWL data range projection")
        if kind == "object_enumeration":
            return t.ObjectEnumeration(
                tuple(
                    self.entity(raw, depth + 1, ("named_individual",))
                    for raw in self.records(item.get("values"))
                )
            )
        prop = self.property(item.get("property"), depth + 1)
        if kind in ("object_quantifier", "data_quantifier"):
            quant = self.text(item.get("quantifier"))
            if quant not in ("some", "all"):
                raise ResourceError("Invalid OWL restriction quantifier")
            if kind == "object_quantifier" and isinstance(prop, t.ObjectProperty):
                return t.ObjectQuantifier(
                    cast(Literal["some", "all"], quant),
                    prop,
                    self.class_expression(item.get("filler"), depth + 1),
                )
            if kind == "data_quantifier" and isinstance(prop, t.DataProperty):
                return t.DataQuantifier(
                    cast(Literal["some", "all"], quant),
                    prop,
                    self.data_range(item.get("filler"), depth + 1),
                )
        if kind in ("object_cardinality", "data_cardinality"):
            op = self.text(item.get("operator"))
            if op not in ("min", "max", "exact"):
                raise ResourceError("Invalid OWL cardinality operator")
            count = result_counter(item.get("count"), self.budget, 2**31 - 1)
            qualified = self.boolean(item.get("qualified"))
            if kind == "object_cardinality" and isinstance(prop, t.ObjectProperty):
                return t.ObjectCardinality(
                    cast(Literal["min", "max", "exact"], op),
                    count,
                    prop,
                    self.class_expression(item.get("filler"), depth + 1),
                    qualified,
                )
            if kind == "data_cardinality" and isinstance(prop, t.DataProperty):
                return t.DataCardinality(
                    cast(Literal["min", "max", "exact"], op),
                    count,
                    prop,
                    self.data_range(item.get("filler"), depth + 1),
                    qualified,
                )
        if kind == "object_self" and isinstance(prop, t.ObjectProperty):
            return t.ObjectSelf(prop)
        if kind == "object_value" and isinstance(prop, t.ObjectProperty):
            return t.ObjectValue(
                prop, self.entity(item.get("value"), depth + 1, ("named_individual",))
            )
        if kind == "data_value" and isinstance(prop, t.DataProperty):
            return t.DataValue(prop, self.literal(item.get("value"), depth + 1))
        raise ResourceError("OWL restriction property role is unsupported")

    def class_expression(self, value: object, depth: int) -> t.ClassExpression:
        """Require the class-expression branch of the finite decoder."""
        return cast(t.ClassExpression, self.expression(value, depth))

    def data_range(self, value: object, depth: int) -> t.DataRange:
        """Require the data-range branch of the finite decoder."""
        return cast(t.DataRange, self.expression(value, depth, data=True))

    def axiom(self, value: object) -> t.ProjectedAxiom:
        """Decode only explicitly represented axiom families and annotations."""
        kind, item = self.node(value, 1)
        annotations = self.annotations(item.get("annotations"), 2)
        if kind == "declaration":
            return t.Declaration(
                self.entity(
                    item.get("entity"),
                    2,
                    (
                        "class",
                        "object_property",
                        "data_property",
                        "annotation_property",
                        "datatype",
                        "named_individual",
                    ),
                ),
                annotations,
            )
        if kind == "subclass":
            return t.SubClassOf(
                self.class_expression(item.get("sub_class"), 2),
                self.class_expression(item.get("super_class"), 2),
                annotations,
            )
        if kind == "class_relation":
            relation = self.text(item.get("relation"))
            if relation not in ("equivalent", "disjoint"):
                raise ResourceError("Unsupported OWL class relation")
            return t.ClassRelation(
                cast(Literal["equivalent", "disjoint"], relation),
                tuple(
                    self.class_expression(raw, 2)
                    for raw in self.records(item.get("classes"))
                ),
                annotations,
            )
        if kind == "class_assertion":
            return t.ClassAssertion(
                self.entity(item.get("individual"), 2, ("named_individual",)),
                self.class_expression(item.get("expression"), 2),
                annotations,
            )
        if kind == "datatype_definition":
            return t.DatatypeDefinition(
                self.entity(item.get("datatype"), 2, ("datatype",)),
                self.data_range(item.get("definition"), 2),
                annotations,
            )
        if kind == "property_domain_range":
            role = self.text(item.get("role"))
            if role not in ("domain", "range"):
                raise ResourceError("Unsupported OWL property axiom")
            prop = self.property(item.get("property"), 2)
            expression = (
                self.data_range(item.get("expression"), 2)
                if isinstance(prop, t.DataProperty) and role == "range"
                else self.class_expression(item.get("expression"), 2)
            )
            return t.PropertyDomainRange(
                cast(Literal["domain", "range"], role), prop, expression, annotations
            )
        if kind == "subproperty":
            sub, sup = (
                self.property(item.get("sub_property"), 2),
                self.property(item.get("super_property"), 2),
            )
            if type(sub) is not type(sup):
                raise ResourceError("OWL subproperty kinds differ")
            return t.SubPropertyOf(sub, sup, annotations)
        if kind == "annotation_assertion":
            subject = self.annotation_value(item.get("subject"), 2)
            if not isinstance(subject, t.IriValue):
                raise ResourceError("OWL annotation subject is not a named IRI")
            return t.AnnotationAssertion(
                self.iri(item.get("property")),
                subject,
                self.annotation_value(item.get("value"), 2),
                annotations,
            )
        raise ResourceError("Unsupported OWL axiom protocol projection")
