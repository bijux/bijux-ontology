"""Owned mapping selections, declared roles and maintained native construction."""

from collections.abc import Sequence
from typing import cast

from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.mappings.composition.types import MappingOccurrence
from bijux_ontology.vocabulary.mappings.types import MappingSet

from ..axioms.changes.checking import prepare_axiom_change
from ..axioms.changes.preconditions import axiom_base
from ..axioms.changes.types import AxiomCheck
from ..axioms.inspection import inspect_axioms
from ..axioms.types import (
    AxiomInspection,
    ClassRelation,
    DataProperty,
    Declaration,
    EntityKind,
    NamedEntity,
    ObjectProperty,
    ProjectedAxiom,
    SubClassOf,
    SubPropertyOf,
)
from ..backend.types import OwlProfileBackend
from ..documents.sources.native import prepare_owl_document
from ..documents.types import OwlDocument
from ..integration.preparation import reserve
from ..integration.types import TheoryComponent
from .types import BridgeInterpretation, BridgeKind, MappingBridge

_KINDS: dict[str, EntityKind] = {
    "equivalent_classes": "class",
    "subclass": "class",
    "superclass": "class",
    "equivalent_object_properties": "object_property",
    "sub_object_property": "object_property",
    "super_object_property": "object_property",
    "equivalent_data_properties": "data_property",
    "sub_data_property": "data_property",
    "super_data_property": "data_property",
}
_ENDPOINT_TYPES = frozenset(str(value) for value in (RDFS.Resource, SKOS.Concept))
_OWL_TYPES = {
    "class": frozenset((str(OWL.Class),)),
    "object_property": frozenset((str(OWL.ObjectProperty),)),
    # The supported SSSOM schema expands its data-property enum to DataProperty.
    "data_property": frozenset((str(OWL.DatatypeProperty), str(OWL) + "DataProperty")),
}
_PREDICATE_TYPES = frozenset(
    str(value) for value in (RDF.Property, OWL.ObjectProperty, RDFS.Resource)
)


def capture_interpretations(
    values: Sequence[BridgeInterpretation], mappings: MappingSet, operation: Operation
) -> tuple[BridgeInterpretation, ...]:
    """Capture exact owned occurrences and all caller decisions before execution."""
    raw = capture(values, operation)
    reserve(operation, len(raw) + 1)
    selected: list[BridgeInterpretation] = []
    seen: set[int] = set()
    for value in raw:
        if (
            type(value) is not BridgeInterpretation
            or type(value.occurrence) is not MappingOccurrence
        ):
            raise InputError("Bridge selections require typed original occurrences")
        occurrence = value.occurrence
        if (
            occurrence.mapping_set is not mappings
            or type(occurrence.index) is not int
            or not 0 <= occurrence.index < len(mappings.rows)
        ):
            raise InputError(
                "A bridge occurrence must belong to the supplied mapping set"
            )
        raw_kind: object = value.kind
        raw_reason: object = value.reason
        if type(raw_kind) is not str or type(raw_reason) is not str:
            raise InputError("Bridge interpretation fields require ordinary text")
        bound_text(raw_kind, operation)
        bound_text(raw_reason, operation)
        if raw_kind not in _KINDS or not raw_reason or raw_reason.isspace():
            raise InputError(
                "Select a finite bridge meaning with an explicit approval reason"
            )
        if occurrence.index in seen:
            raise InputError(
                "Select each mapping occurrence once with one explicit meaning"
            )
        seen.add(occurrence.index)
        selected.append(
            BridgeInterpretation(occurrence, cast(BridgeKind, raw_kind), raw_reason)
        )
    return tuple(selected)


def interpretable(
    selected: BridgeInterpretation,
    mappings: MappingSet,
    metadata_unknown: bool,
    operation: Operation,
) -> bool:
    """Do not erase semantic modifiers or unsupported endpoint representations."""
    row = selected.occurrence.row
    reserve(operation, len(row.cells) + len(mappings.uninterpreted) + 1)
    unknown = frozenset(mappings.uninterpreted)
    facet = _OWL_TYPES[_KINDS[selected.kind]]
    return (
        not metadata_unknown
        and "https://w3id.org/sssom/NoTermFound" not in (row.subject, row.object)
        and (
            not row.subject_type
            or row.subject_type in _ENDPOINT_TYPES
            or row.subject_type in facet
        )
        and (
            not row.object_type
            or row.object_type in _ENDPOINT_TYPES
            or row.object_type in facet
        )
        and (not row.predicate_type or row.predicate_type in _PREDICATE_TYPES)
        and not any(
            text and (name in unknown or name in ("predicate_modifier", "other"))
            for name, text in row.cells
        )
    )


def declared_roles(
    inspection: AxiomInspection, operation: Operation
) -> frozenset[tuple[str, str]]:
    """Only actual native declarations qualify a selected original entity facet."""
    reserve(operation, len(inspection.axioms) + 1)
    operation.consume(
        "work",
        sum(
            len(record.axiom.entity.iri) + 1
            for record in inspection.axioms
            if isinstance(record.axiom, Declaration)
        ),
    )
    return frozenset(
        (record.axiom.entity.kind, record.axiom.entity.iri)
        for record in inspection.axioms
        if isinstance(record.axiom, Declaration)
    )


def roles_match(
    selected: BridgeInterpretation,
    subject: frozenset[tuple[str, str]],
    object_: frozenset[tuple[str, str]],
    operation: Operation,
) -> bool:
    """Require each original endpoint to have the caller-selected native facet."""
    kind = _KINDS[selected.kind]
    row = selected.occurrence.row
    operation.consume("work", len(row.subject) + len(row.object) + 2)
    return (kind, row.subject) in subject and (kind, row.object) in object_


def build_bridge(
    backend: OwlProfileBackend,
    selected: BridgeInterpretation,
    index: int,
    operation: Operation,
    heap_mb: int,
) -> MappingBridge:
    """Use existing typed axiom changes, one original row per native source owner."""
    reserve(operation, 16)
    row, meaning = selected.occurrence.row, selected.kind
    role = _KINDS[meaning]
    left, right = NamedEntity(role, row.subject), NamedEntity(role, row.object)
    additions: tuple[ProjectedAxiom, ...] = (Declaration(left, ()),)
    if left != right:
        additions += (Declaration(right, ()),)
    if meaning == "equivalent_classes":
        additions += (ClassRelation("equivalent", (left, right), ()),)
    elif meaning in ("subclass", "superclass"):
        additions += (
            SubClassOf(right, left, ())
            if meaning == "superclass"
            else SubClassOf(left, right, ()),
        )
    else:
        first, second = (
            (ObjectProperty(row.subject), ObjectProperty(row.object))
            if role == "object_property"
            else (DataProperty(row.subject), DataProperty(row.object))
        )
        if meaning.startswith("super_"):
            first, second = second, first
        additions += (SubPropertyOf(first, second, ()),)
        if meaning.startswith("equivalent_"):
            additions += (SubPropertyOf(second, first, ()),)
    identity = "urn:bijux:ontology:mapping-bridge:" + str(index)
    empty = prepare_owl_document(
        "Ontology(<" + identity + ">)",
        format="functional",
        document_iri=identity,
        budget=operation,
    )

    def structure(source: OwlDocument, budget: Budget) -> Report:
        """Inspect actual constructed native axioms with the same operation grant."""
        return inspect_axioms(backend, source, budget=budget, heap_mb=heap_mb).report

    candidate = prepare_axiom_change(
        backend,
        empty,
        expected=axiom_base(empty, budget=operation),
        additions=additions,
        profiles=("owl2-dl",),
        after_checks=(AxiomCheck("native bridge structure", structure),),
        budget=operation,
        heap_mb=heap_mb,
    )
    module = (
        TheoryComponent("mapping-bridge:" + str(index), candidate.source)
        if candidate.report.verified and candidate.source is not None
        else None
    )
    return MappingBridge(selected, candidate, module)
