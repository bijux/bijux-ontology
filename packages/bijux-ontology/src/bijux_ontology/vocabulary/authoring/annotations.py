"""Exact annotation changes and exclusive finite OBO OWL.Axiom attachments."""

from __future__ import annotations

from typing import cast

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.execution.limits import retain_report
from bijux_ontology.rdf.patches.planning import Rejected
from bijux_ontology.rdf.records.types import PatchResource, PatchUnavailable
from bijux_ontology.rdf.roles.classification import classify_statement, declaration_role
from bijux_ontology.vocabulary.obo.reading import _ROLE, read_obo_records

from .types import (
    AnnotationChange,
    QualifiedAnnotationChange,
    VocabularyEditContext,
    _iri,
    _values,
)

_SAFE_STANDARD = frozenset(
    (
        RDFS.label,
        RDFS.comment,
        RDFS.seeAlso,
        RDFS.isDefinedBy,
        OWL.versionInfo,
        OWL.priorVersion,
        OWL.backwardCompatibleWith,
        OWL.incompatibleWith,
    )
)
_ANCHORS = (OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget)


def _predicate(
    state: VocabularyEditContext,
    subject: URIRef,
    predicate: URIRef,
    *,
    known_obo: bool = False,
) -> None:
    """Accepted annotation roles cannot override standard structural semantics."""
    _iri(predicate, state.operation)
    if predicate not in _SAFE_STANDARD and any(
        str(predicate).startswith(str(namespace)) for namespace in (RDF, RDFS, OWL)
    ):
        raise PatchUnavailable(
            "Reserved RDF/OWL predicates require a typed structural operation"
        )
    roles = classify_statement(
        (subject, predicate, Literal("")), budget=state.operation
    )
    if any(
        role.position == "predicate" and role.role == "structural" for role in roles
    ):
        raise PatchUnavailable(
            "Structural relationships cannot be authored as annotations"
        )
    kinds = state.values(predicate, RDF.type)
    if any(
        isinstance(kind, URIRef)
        and (
            declaration_role(kind) in ("object_property", "datatype_property")
            or kind == OWL.FunctionalProperty
        )
        for kind in kinds
    ):
        raise PatchUnavailable(
            "A conflicting logical property kind is not an annotation profile"
        )
    if (
        predicate not in state.annotation_predicates
        and OWL.AnnotationProperty not in kinds
        and not (known_obo and str(predicate) in _ROLE)
    ):
        raise PatchUnavailable(
            "Annotation changes require an explicit accepted predicate profile"
        )


def change_annotation(state: VocabularyEditContext, edit: AnnotationChange) -> None:
    """Replace only the exact selected set; never orphan an existing attachment."""
    _iri(edit.subject, state.operation)
    _predicate(state, edit.subject, edit.predicate)
    previous = tuple(
        state.ref(value) for value in _values(edit.previous, state.operation)
    )
    values = tuple(state.ref(value) for value in _values(edit.values, state.operation))
    state.operation.consume("states", len(previous) + len(values))
    if len(set(previous)) != len(previous) or len(set(values)) != len(values):
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Annotation sets cannot repeat the same RDF term",
        )
    if set(previous) != set(state.values(edit.subject, edit.predicate)):
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Annotation values differ from the exact before-set",
        )
    removed = set(previous) - set(values)
    for quad in state.current:
        state.operation.consume("work")
        if (
            quad.graph == state.graph_ref
            and quad.predicate == OWL.annotatedSource
            and quad.value == edit.subject
            and edit.predicate in state.values(quad.subject, OWL.annotatedProperty)
            and removed.intersection(state.values(quad.subject, OWL.annotatedTarget))
        ):
            raise PatchUnavailable(
                "Remove qualified attachments before changing their base assertion"
            )
    for value in previous:
        state.drop(edit.subject, edit.predicate, value)
    for value in values:
        state.put(edit.subject, edit.predicate, value)


def change_qualified(
    state: VocabularyEditContext, edit: QualifiedAnnotationChange
) -> None:
    """Support exact OBO annotation anchors without merging qualifier resources."""
    _iri(edit.subject, state.operation)
    _iri(edit.predicate, state.operation)
    if str(edit.predicate) not in _ROLE:
        raise PatchUnavailable(
            "Qualified annotation authoring supports only native OBO record predicates"
        )
    _predicate(state, edit.subject, edit.predicate, known_obo=True)
    value = state.ref(edit.value)
    qualifiers = []
    for predicate, item in edit.qualifiers:
        state.operation.consume("work")
        _predicate(state, edit.subject, predicate)
        qualifiers.append((predicate, state.ref(item)))
    state.operation.consume("states", len(qualifiers))
    if len(set(qualifiers)) != len(qualifiers):
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Qualifier pairs cannot repeat exact assertions",
        )
    node: PatchResource
    if edit.action == "add":
        node = state.fresh()
        state.put(edit.subject, edit.predicate, value)
        for predicate, output in (
            (RDF.type, OWL.Axiom),
            (OWL.annotatedSource, edit.subject),
            (OWL.annotatedProperty, edit.predicate),
            (OWL.annotatedTarget, value),
            *qualifiers,
        ):
            state.put(node, predicate, output)
        return
    assert edit.attachment is not None
    node = cast(PatchResource, state.ref(edit.attachment))
    records = read_obo_records(state.view(), str(edit.subject), budget=state.operation)
    retain_report(records.report, state.operation, state.issues)
    state.children.append(records.report)
    matches = [
        attachment
        for attachment in records.attachments
        if (
            attachment.node is edit.attachment
            if isinstance(edit.attachment, BNode)
            else attachment.node == edit.attachment
        )
    ]
    if len(matches) != 1 or not matches[0].complete or matches[0].status != "attached":
        raise PatchUnavailable(
            "The selected annotation lacks complete exact owned attachment evidence"
        )
    anchors = {
        RDF.type: (OWL.Axiom,),
        OWL.annotatedSource: (edit.subject,),
        OWL.annotatedProperty: (edit.predicate,),
        OWL.annotatedTarget: (value,),
    }
    for predicate, expected in anchors.items():
        if set(state.values(node, predicate)) != set(expected):
            raise PatchUnavailable(
                "The finite OWL.Axiom anchors are missing or ambiguous"
            )
    statements = state.outgoing(node)
    actual = {
        (quad.predicate, quad.value)
        for quad in statements
        if quad.predicate not in (*_ANCHORS, RDF.type)
    }
    if actual != set(qualifiers):
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Qualified annotation differs from the exact qualifier inventory",
        )
    state.operation.consume("work", len(state.captured.witness.graphs))
    if node in state.captured.witness.graphs:
        raise PatchUnavailable("The annotation resource also names an owned graph")
    # A foreign incoming use or another graph's body makes deletion shared.
    for quad in state.current:
        state.operation.consume("work")
        if (
            quad.value == node
            or quad.predicate == node
            or quad.subject == node
            and quad.graph != state.graph_ref
        ):
            raise PatchUnavailable(
                "The selected qualified annotation resource is still shared"
            )
    for quad in statements:
        state.drop(node, quad.predicate, quad.value)
