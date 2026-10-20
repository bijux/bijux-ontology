"""Exact leaf qualifier edits consumed by the ordinary vocabulary transaction."""

from typing import Protocol, cast

from rdflib import BNode, URIRef
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import IssueCollector, retain_report
from bijux_ontology.rdf.patches.planning import Captured, Rejected
from bijux_ontology.rdf.records.types import (
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from .reading import _bound, _same, inspect_annotation_graph
from .types import AnnotationValue, AnnotationValueEdit


class AnnotationEditContext(Protocol):
    """Existing transaction capabilities, with no graph copier or nested executor."""

    source: PreparedSource
    graph_name: URIRef | BNode | None
    graph_ref: PatchResource | None
    operation: Operation
    captured: Captured
    issues: IssueCollector
    children: list[Report]
    current: set[PatchQuad]
    annotation_predicates: frozenset[URIRef]

    def ref(self, value: AnnotationValue) -> PatchValue:
        """Qualify an original exact RDF term using the existing captured witness."""
        ...

    def drop(
        self, subject: PatchResource, predicate: URIRef, value: PatchValue
    ) -> None:
        """Remove one exact original assertion from the selected overlay."""
        ...

    def put(self, subject: PatchResource, predicate: URIRef, value: PatchValue) -> None:
        """Add one exact replacement assertion to the selected overlay."""
        ...


def change_annotation_value(
    state: AnnotationEditContext, edit: AnnotationValueEdit
) -> None:
    """Replace a witnessed leaf, preserving all anchors and unrelated qualifiers.

    Required shared, cyclic, malformed or child-qualified targets remain unknown.
    Cross-graph bodies and arbitrary incoming uses prevent an exclusive targeted
    replacement. All checks use original RDF witnesses plus the current overlay;
    an earlier edit cannot silently invalidate the exact before-precondition.
    """
    for statement in (edit.root, edit.statement):
        _bound(statement, state.operation)
    for node in edit.attachments:
        state.ref(node)
    replacement = state.ref(edit.replacement)
    selected = inspect_annotation_graph(
        state.source,
        (edit.root,),
        graph=state.graph_name,
        annotation_predicates=tuple(state.annotation_predicates),
        budget=state.operation,
    )
    retain_report(selected.report, state.operation, state.issues)
    state.children.append(selected.report)
    if not selected.report.verified:
        raise PatchUnavailable(
            "Annotation edit requires a complete exact attachment interpretation"
        )
    matches = []
    for path in selected.paths:
        state.operation.consume("work")
        if (
            len(path.attachments) == len(edit.attachments)
            and all(
                _same(left, right)
                for left, right in zip(path.attachments, edit.attachments, strict=True)
            )
            and _same(path.statement.subject, edit.statement.subject)
            and path.statement.predicate == edit.statement.predicate
            and _same(path.statement.value, edit.statement.value)
        ):
            state.operation.consume("states")
            matches.append(path)
    if len(matches) != 1:
        raise PatchUnavailable(
            "Qualifier edit lacks one exact selected root/path witness"
        )
    owner = cast(PatchResource, state.ref(edit.statement.subject))
    if not _same(edit.statement.subject, edit.attachments[-1]):
        raise PatchUnavailable(
            "A qualifier edit must target the selected attachment itself"
        )
    for attachment in selected.attachments:
        state.operation.consume("work")
        if attachment.annotated == edit.statement:
            raise PatchUnavailable(
                "A qualified child annotation prevents leaf replacement"
            )
    old = state.ref(edit.statement.value)
    previous = PatchQuad(owner, edit.statement.predicate, old, state.graph_ref)
    if previous not in state.current:
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Qualifier differs from its exact pending before-state",
        )
    # Every proof assertion must still exist; roots/anchors cannot be invalidated
    # by earlier edits in this same ordinary transaction.
    proof_nodes = set(edit.attachments)
    for statement in selected.statements:
        state.operation.consume("work")
        if statement == edit.root or statement.subject in proof_nodes:
            proof = PatchQuad(
                cast(PatchResource, state.ref(statement.subject)),
                statement.predicate,
                state.ref(statement.value),
                state.graph_ref,
            )
            if proof not in state.current:
                raise PatchUnavailable(
                    "An earlier edit changed the annotation path before-state"
                )
    for quad in state.current:
        state.operation.consume("work")
        if (
            quad.value == owner
            or quad.subject == owner
            and quad.graph != state.graph_ref
            or quad.graph == owner
        ):
            raise PatchUnavailable(
                "Shared or cross-graph ownership prevents targeted replacement"
            )
        if (
            quad.graph == state.graph_ref
            and quad.predicate == OWL.annotatedSource
            and quad.value == owner
        ):
            raise PatchUnavailable(
                "Nested annotation ownership prevents a leaf replacement"
            )
    state.drop(owner, edit.statement.predicate, old)
    state.put(owner, edit.statement.predicate, replacement)
