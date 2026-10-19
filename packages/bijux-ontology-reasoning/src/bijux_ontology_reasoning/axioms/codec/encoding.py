"""Bounded construction encoding for the existing finite OWL object model."""

from collections.abc import Sequence
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from .. import types as t
from .decoding import ProjectionDecoder


class ConstructionEncoder:
    """Qualify explicit typed roles before the maintained native factory runs."""

    def __init__(self, budget: Budget) -> None:
        """Use one operation allowance for every nested construction record."""
        self.budget = budget

    def text(self, value: str, *, iri: bool = False) -> str:
        """Bound immutable caller text before parsing or dictionary retention."""
        if type(value) is not str:
            raise InputError("OWL construction requires plain strings")
        bound_text(value, self.budget)
        return str(absolute_iri(value)) if iri else value

    def sequence(self, values: object) -> tuple[object, ...]:
        """Require an immutable finite constructor inventory and prepay references."""
        if type(values) is not tuple:
            raise InputError("OWL constructor members require exact tuples")
        result = cast(tuple[object, ...], values)
        self.budget.consume("states", len(result))
        self.budget.consume("bytes", 64 + len(result) * 32)
        return result

    def nodes(self, values: object, depth: int) -> list[dict[str, object]]:
        """Encode a prepaid immutable nested inventory."""
        return [self.node(value, depth) for value in self.sequence(values)]

    def node(self, raw: object, depth: int = 1) -> dict[str, object]:
        """Encode only supported concrete model classes, never arbitrary dataclasses."""
        self.budget.consume("work")
        self.budget.consume("states")
        self.budget.consume("bytes", 384)
        if depth > min(64, self.budget.limits.max_depth // 4):
            raise LimitExceeded("OWL constructor exceeds its bounded nesting")
        nested = depth + 1
        if type(raw) is t.NamedEntity:
            return {
                "kind": "entity",
                "role": self.text(raw.kind),
                "iri": self.text(raw.iri, iri=True),
            }
        if type(raw) is t.NativeLiteral:
            language = None if raw.language is None else self.text(raw.language)
            datatype = self.text(raw.datatype, iri=True)
            if (language is not None) != (
                datatype == "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
            ):
                raise InputError("Native literal language and datatype disagree")
            if language == "":
                raise InputError("Native literal language cannot be empty")
            return {
                "kind": "literal",
                "lexical": self.text(raw.lexical),
                "datatype": datatype,
                "language": language,
            }
        if type(raw) is t.IriValue:
            return {"kind": "iri", "iri": self.text(raw.iri, iri=True)}
        if type(raw) is t.ObjectProperty:
            if type(raw.inverse) is not bool:
                raise InputError("Object inverse requires an exact boolean")
            return {
                "kind": "object_property",
                "iri": self.text(raw.iri, iri=True),
                "inverse": raw.inverse,
            }
        if type(raw) is t.DataProperty:
            return {"kind": "data_property", "iri": self.text(raw.iri, iri=True)}
        if type(raw) is t.OwlAnnotation:
            return {
                "kind": "annotation",
                "property": self.text(raw.property, iri=True),
                "value": self.node(raw.value, nested),
                "annotations": self.nodes(raw.annotations, nested),
            }
        if type(raw) in (t.ClassJunction, t.DataJunction):
            assert isinstance(raw, t.ClassJunction | t.DataJunction)
            if (
                raw.operator not in ("intersection", "union")
                or len(self.sequence(raw.operands)) < 2
            ):
                raise InputError("OWL junction requires two or more operands")
            return {
                "kind": "object_junction"
                if type(raw) is t.ClassJunction
                else "data_junction",
                "operator": raw.operator,
                "operands": self.nodes(raw.operands, nested),
            }
        if type(raw) in (t.ClassComplement, t.DataComplement):
            assert isinstance(raw, t.ClassComplement | t.DataComplement)
            return {
                "kind": "object_complement"
                if type(raw) is t.ClassComplement
                else "data_complement",
                "operand": self.node(raw.operand, nested),
            }
        if type(raw) in (t.ObjectEnumeration, t.DataEnumeration):
            assert isinstance(raw, t.ObjectEnumeration | t.DataEnumeration)
            if not self.sequence(raw.values):
                raise InputError("OWL enumeration requires actual members")
            return {
                "kind": "object_enumeration"
                if type(raw) is t.ObjectEnumeration
                else "data_enumeration",
                "values": self.nodes(raw.values, nested),
            }
        if type(raw) is t.DatatypeFacet:
            return {
                "kind": "facet",
                "iri": self.text(raw.iri, iri=True),
                "value": self.node(raw.value, nested),
            }
        if type(raw) is t.DatatypeRestriction:
            if not self.sequence(raw.facets):
                raise InputError("Datatype restriction requires explicit facets")
            return {
                "kind": "datatype_restriction",
                "datatype": self.node(raw.datatype, nested),
                "facets": self.nodes(raw.facets, nested),
            }
        if type(raw) in (t.ObjectQuantifier, t.DataQuantifier):
            assert isinstance(raw, t.ObjectQuantifier | t.DataQuantifier)
            if raw.quantifier not in ("some", "all"):
                raise InputError("Choose an actual OWL quantifier")
            return {
                "kind": "object_quantifier"
                if type(raw) is t.ObjectQuantifier
                else "data_quantifier",
                "quantifier": raw.quantifier,
                "property": self.node(raw.property, nested),
                "filler": self.node(raw.filler, nested),
            }
        if type(raw) in (t.ObjectCardinality, t.DataCardinality):
            assert isinstance(raw, t.ObjectCardinality | t.DataCardinality)
            if (
                raw.operator not in ("min", "max", "exact")
                or type(raw.count) is not int
                or not 0 <= raw.count <= 2**31 - 1
                or type(raw.qualified) is not bool
            ):
                raise InputError("Invalid explicit OWL cardinality")
            return {
                "kind": "object_cardinality"
                if type(raw) is t.ObjectCardinality
                else "data_cardinality",
                "operator": raw.operator,
                "count": raw.count,
                "qualified": raw.qualified,
                "property": self.node(raw.property, nested),
                "filler": self.node(raw.filler, nested),
            }
        if type(raw) in (t.ObjectValue, t.DataValue):
            assert isinstance(raw, t.ObjectValue | t.DataValue)
            return {
                "kind": "object_value" if type(raw) is t.ObjectValue else "data_value",
                "property": self.node(raw.property, nested),
                "value": self.node(raw.value, nested),
            }
        if type(raw) is t.ObjectSelf:
            return {"kind": "object_self", "property": self.node(raw.property, nested)}
        kind: str
        values: dict[str, object]
        if type(raw) is t.Declaration:
            kind, values = "declaration", {"entity": self.node(raw.entity, nested)}
        elif type(raw) is t.SubClassOf:
            kind, values = (
                "subclass",
                {
                    "sub_class": self.node(raw.sub_class, nested),
                    "super_class": self.node(raw.super_class, nested),
                },
            )
        elif type(raw) is t.ClassRelation:
            kind, values = (
                "class_relation",
                {
                    "relation": self.text(raw.kind),
                    "classes": self.nodes(raw.classes, nested),
                },
            )
        elif type(raw) is t.ClassAssertion:
            kind, values = (
                "class_assertion",
                {
                    "individual": self.node(raw.individual, nested),
                    "expression": self.node(raw.expression, nested),
                },
            )
        elif type(raw) is t.DatatypeDefinition:
            kind, values = (
                "datatype_definition",
                {
                    "datatype": self.node(raw.datatype, nested),
                    "definition": self.node(raw.definition, nested),
                },
            )
        elif type(raw) is t.PropertyDomainRange:
            kind, values = (
                "property_domain_range",
                {
                    "role": self.text(raw.kind),
                    "property": self.node(raw.property, nested),
                    "expression": self.node(raw.expression, nested),
                },
            )
        elif type(raw) is t.SubPropertyOf:
            kind, values = (
                "subproperty",
                {
                    "sub_property": self.node(raw.sub_property, nested),
                    "super_property": self.node(raw.super_property, nested),
                },
            )
        elif type(raw) is t.AnnotationAssertion:
            kind, values = (
                "annotation_assertion",
                {
                    "property": self.text(raw.property, iri=True),
                    "subject": self.node(raw.subject, nested),
                    "value": self.node(raw.value, nested),
                },
            )
        else:
            raise InputError("Unsupported concrete OWL constructor")
        values.update(kind=kind, annotations=self.nodes(raw.annotations, nested))
        return values

    def axioms(
        self, values: Sequence[t.ProjectedAxiom]
    ) -> tuple[tuple[t.ProjectedAxiom, ...], list[dict[str, object]]]:
        """Capture typed constructions through the existing role-aware decoder."""
        if not isinstance(values, Sequence) or isinstance(
            cast(object, values), str | bytes
        ):
            raise InputError("Axiom changes require a finite typed sequence")
        self.budget.consume("states", len(values) * 2)
        self.budget.consume("bytes", 64 + len(values) * 64)
        captured: list[t.ProjectedAxiom] = []
        wire = []
        decoder = ProjectionDecoder(self.budget)
        for index in range(len(values)):
            encoded = self.node(values[index])
            captured.append(decoder.axiom(encoded))
            wire.append(encoded)
        return tuple(captured), wire
