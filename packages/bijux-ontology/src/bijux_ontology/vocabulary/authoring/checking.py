"""Checked native vocabulary transactions over exact owned RDF dataset patches."""

from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.processes.transport import checkpoint
from bijux_ontology.rdf.migrations.candidates import (
    MigrationCandidate,
    MigrationCheck,
    finish_migration,
)
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import Captured, Rejected, capture_source
from bijux_ontology.rdf.patches.types import (
    DatasetPatch,
    PatchBlank,
    PatchBlankBinding,
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
    bound_term,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.vocabulary.annotations.graphs.types import (
    AnnotationLiteralRepair,
    AnnotationValueEdit,
)
from bijux_ontology.vocabulary.browsing.inspection import (
    VocabularyView,
    inspect_vocabulary,
)
from bijux_ontology.vocabulary.sources.types import OwnedSource
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .types import (
    AnnotationChange,
    AnnotationValue,
    CreateSkosResource,
    DeprecateResource,
    PreferredLabelChange,
    QualifiedAnnotationChange,
    SchemeMembership,
    VocabularyEdit,
    _iri,
)


class _Transaction:
    """One charged patch overlay; all original terms keep their captured identities."""

    def __init__(
        self,
        source: PreparedSource,
        graph: URIRef | BNode | None | Absent,
        captured: Captured,
        operation: Operation,
        issues: IssueCollector,
        predicates: tuple[URIRef, ...],
    ) -> None:
        self.source, self.captured, self.operation, self.issues = (
            source,
            captured,
            operation,
            issues,
        )
        self.children: list[Report] = []
        self._view: VocabularyView | None = None
        self._fresh = 0
        if source.is_dataset and graph is ABSENT:
            raise PatchUnavailable(
                "Dataset authoring requires an explicit physical graph"
            )
        self.graph_name = graph if graph is not ABSENT else None
        self.graph_ref = (
            cast(PatchResource, self.ref(self.graph_name))
            if self.graph_name is not None
            else None
        )
        if (
            captured.witness.kind == "named"
            and self.graph_ref != captured.witness.graph_selection
        ):
            raise PatchUnavailable("Authoring graph conflicts with the pinned source")
        if not source.is_dataset and self.graph_ref is not None:
            raise PatchUnavailable("Graph inputs cannot authorize a named graph edit")
        if self.graph_ref is not None and self.graph_ref not in captured.witness.graphs:
            raise PatchUnavailable("The selected graph is absent from the source")
        self.graph: Graph = source._read(
            (self.graph_name if self.graph_name is not None else "@default")
            if source.is_dataset
            else None,
            budget=operation,
        )
        operation.consume("states", len(captured.witness.quads))
        operation.consume("work", len(captured.witness.quads))
        self.current = set(captured.witness.quads)
        self.annotation_predicates = frozenset(predicates)

    def ref(self, value: AnnotationValue) -> PatchValue:
        """Use existing portable term rules and actual captured blank witnesses."""
        bound_node(value, self.operation)
        if isinstance(value, BNode):
            return self.captured.reference(value)
        if isinstance(value, Literal):
            return PatchLiteral(
                str(value), str(literal_datatype(value)), value.language
            )
        return URIRef(value)

    def values(
        self, subject: PatchResource, predicate: URIRef
    ) -> tuple[PatchValue, ...]:
        """Read actual pending values without building another graph or index."""
        values = []
        for quad in self.current:
            self.operation.consume("work")
            if (
                quad.graph == self.graph_ref
                and quad.subject == subject
                and quad.predicate == predicate
            ):
                self.operation.consume("states")
                values.append(quad.value)
        return tuple(values)

    def outgoing(self, subject: PatchResource) -> tuple[PatchQuad, ...]:
        """Select exact outgoing statements in the authored physical graph."""
        selected = []
        for quad in self.current:
            self.operation.consume("work")
            if quad.subject == subject and quad.graph == self.graph_ref:
                self.operation.consume("states")
                selected.append(quad)
        return tuple(selected)

    def put(self, subject: PatchResource, predicate: URIRef, value: PatchValue) -> None:
        """Add one qualified exact assertion to the transaction overlay."""
        self.operation.consume("work")
        self.operation.consume("states")
        self.operation.consume("triples")
        for term in (subject, predicate, value):
            bound_term(term, self.operation)
        self.current.add(PatchQuad(subject, predicate, value, self.graph_ref))

    def drop(
        self, subject: PatchResource, predicate: URIRef, value: PatchValue
    ) -> None:
        """Remove only an explicit selected fact, never a recursive resource body."""
        self.operation.consume("work")
        for term in (subject, predicate, value):
            bound_term(term, self.operation)
        self.current.discard(PatchQuad(subject, predicate, value, self.graph_ref))

    def fresh(self) -> PatchBlank:
        """Allocate a new attachment token disjoint from captured base references."""
        self.operation.consume("work")
        self.operation.consume("states")
        self.operation.consume("bytes", 64)
        node = PatchBlank("annotation:" + str(self._fresh))
        self._fresh += 1
        return node

    def view(self) -> VocabularyView:
        """Reuse standard owned vocabulary indexing with original blank identities."""
        if self._view is None:
            vocabulary = Vocabulary._from_owned(
                OwnedSource(
                    self.source,
                    self.graph,
                    (self.graph_name if self.graph_name is not None else "@default")
                    if self.source.is_dataset
                    else None,
                ),
                (),
                (),
                self.operation.limits,
            )
            self._view = inspect_vocabulary(
                vocabulary, scopes=("local",), budget=self.operation
            )
        return self._view


def author_vocabulary(
    source: PreparedSource,
    edits: Sequence[VocabularyEdit],
    *,
    graph: URIRef | BNode | None | Absent = ABSENT,
    annotation_predicates: tuple[URIRef, ...] = (),
    before_checks: Sequence[MigrationCheck] = (),
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> MigrationCandidate:
    """Prepare typed vocabulary changes, retaining exact before/after contract truth.

    Datasets require a physical graph. Annotation predicates need explicit caller
    acceptance or a supplied annotation-property declaration; reserved structural
    predicates cannot be reinterpreted through that profile. Original snapshots
    stay unchanged. QualifiedAnnotationChange uses the finite OBO OWL.Axiom
    dialect; AnnotationValueEdit requires an explicitly inspected annotation graph.
    After checks remain mandatory and explicit accepted export uses the existing
    migration gate; a reviewable candidate is not an approved publication.
    """
    from bijux_ontology.vocabulary.annotations.graphs.changes import (
        change_annotation_value,
    )
    from bijux_ontology.vocabulary.annotations.hygiene.changes import (
        repair_annotation_literal,
    )

    from .annotations import change_annotation, change_qualified
    from .skos import change_skos

    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    patch = None
    transformed = None
    checked = 0
    try:
        if graph is not ABSENT and graph is not None:
            if not isinstance(graph, URIRef | BNode):
                raise InputError("Authoring graphs require exact RDF resource terms")
            bound_node(graph, operation)
        supplied = capture_items(edits, object, operation)
        if not supplied:
            raise PatchUnavailable("A required authoring transaction has no edits")
        predicates = capture_items(annotation_predicates, URIRef, operation)
        for predicate in predicates:
            _iri(predicate, operation)
        operation.consume("states", len(predicates))
        captured = capture_source(source, "base:", operation)
        state = _Transaction(source, graph, captured, operation, issues, predicates)
        children = state.children
        for edit in supplied:
            operation.consume("work")
            if isinstance(edit, AnnotationChange):
                change_annotation(state, edit)
            elif isinstance(edit, QualifiedAnnotationChange):
                change_qualified(state, edit)
            elif isinstance(edit, AnnotationValueEdit):
                change_annotation_value(state, edit)
            elif isinstance(edit, AnnotationLiteralRepair):
                repair_annotation_literal(state, edit)
            elif isinstance(
                edit,
                CreateSkosResource
                | PreferredLabelChange
                | SchemeMembership
                | DeprecateResource,
            ):
                change_skos(state, edit)
            else:
                raise InputError("Expected a typed vocabulary edit")
            checked += 1
        operation.consume("states", len(state.current) + len(captured.witness.quads))
        operation.consume("work", len(state.current) + len(captured.witness.quads))
        before = set(captured.witness.quads)
        target = replace(captured.witness, quads=tuple(state.current))
        patch = DatasetPatch(
            captured.witness,
            target,
            removals=tuple(before - state.current),
            additions=tuple(state.current - before),
        )
        bindings = []
        seen: set[str] = set()
        for node, reference in captured.owned.values():
            operation.consume("work")
            if reference.token not in seen:
                operation.consume("states")
                seen.add(reference.token)
                bindings.append(PatchBlankBinding(reference.token, node))
        checkpoint(operation.deadline, cancel)
        remaining = operation.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Vocabulary authoring exceeded its deadline")
        transformed = apply_rdf_patch(
            source,
            patch,
            blank_bindings=tuple(bindings),
            budget=operation,
            timeout=remaining,
            cancel=cancel,
        )
        issues.extend(transformed.report.issues)
        children.append(transformed.report)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (Rejected, PatchUnavailable) as error:
        try:
            issues.append(
                Issue(
                    "vocabulary.authoring_rejected"
                    if isinstance(error, Rejected)
                    else "vocabulary.authoring_unverified",
                    str(error),
                    kind="finding" if isinstance(error, Rejected) else "unverified",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    raw = Report(
        "vocabulary.authoring",
        checked,
        tuple(issues),
        requested=max(1, checked),
        allow_empty=True,
        children=tuple(children),
        regime="typed-vocabulary-authoring",
    )
    return finish_migration(
        source,
        Transformation(
            transformed.source if transformed is not None and raw.verified else None,
            raw,
        ),
        patch,
        before_checks=before_checks,
        after_checks=after_checks,
        budget=operation,
    )
