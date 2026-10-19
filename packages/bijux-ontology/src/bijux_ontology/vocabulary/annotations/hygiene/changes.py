"""Exact literal and reifier-target changes inside the existing transaction."""

from typing import cast

from rdflib import Literal
from rdflib.namespace import OWL

from bijux_ontology.limits import retain_report
from bijux_ontology.rdf.patches.planning import Rejected
from bijux_ontology.rdf.patches.types import PatchQuad, PatchResource, PatchUnavailable
from bijux_ontology.rdf.terms import literal_datatype
from bijux_ontology.vocabulary.annotations.graphs.changes import AnnotationEditContext
from bijux_ontology.vocabulary.annotations.graphs.reading import (
    _bound,
    _physical,
    _Read,
    _same,
    inspect_annotation_graph,
)
from bijux_ontology.vocabulary.annotations.graphs.types import AnnotationLiteralRepair


def repair_annotation_literal(
    state: AnnotationEditContext, edit: AnnotationLiteralRepair
) -> None:
    """Change one actual assertion and every direct OWL target anchor consistently.

    Reifier identities, qualifier assertions and resource bodies survive unchanged.
    Cyclic, shared, cross-graph or malformed ownership cannot qualify this profile.
    A replacement cannot collapse an existing distinct assertion. The original
    language and intrinsic datatype are preserved, without lexical normalization.
    """
    path = edit.path
    for statement in (path.root, path.statement):
        _bound(statement, state.operation)
    for node in path.attachments:
        state.ref(node)
    value = path.statement.value
    if not isinstance(value, Literal):
        raise PatchUnavailable("Only literal annotation assertions can be repaired")
    if (value.language or "").lower() != (
        edit.replacement.language or ""
    ).lower() or literal_datatype(value) != literal_datatype(edit.replacement):
        raise PatchUnavailable("A lexical repair cannot change language or datatype")
    replacement, old = state.ref(edit.replacement), state.ref(value)
    selected = inspect_annotation_graph(
        state.source,
        (path.root,),
        graph=state.graph_name,
        annotation_predicates=tuple(state.annotation_predicates),
        budget=state.operation,
    )
    retain_report(selected.report, state.operation, state.issues)
    state.children.append(selected.report)
    if not selected.report.verified:
        raise PatchUnavailable(
            "Literal repair requires a complete annotation attachment graph"
        )
    _, physical = _physical(state.source, state.graph_name, state.operation)
    reader = _Read(physical, tuple(state.annotation_predicates), state.operation)
    if not reader.accepted(path.statement):
        raise PatchUnavailable(
            "Literal repair requires an accepted annotation predicate"
        )
    matches = 0
    for actual in selected.paths:
        state.operation.consume("work", len(actual.attachments) + 1)
        _bound(actual.statement, state.operation)
        if (
            len(actual.attachments) == len(path.attachments)
            and all(
                _same(left, right)
                for left, right in zip(
                    actual.attachments, path.attachments, strict=True
                )
            )
            and _same(actual.statement.subject, path.statement.subject)
            and actual.statement.predicate == path.statement.predicate
            and _same(actual.statement.value, path.statement.value)
        ):
            matches += 1
    if matches != 1:
        raise PatchUnavailable(
            "Literal repair requires one original owned assertion path"
        )
    owner = cast(PatchResource, state.ref(path.statement.subject))
    previous = PatchQuad(owner, path.statement.predicate, old, state.graph_ref)
    target = PatchQuad(owner, path.statement.predicate, replacement, state.graph_ref)
    if previous not in state.current:
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Literal repair differs from the pending before-state",
        )
    if replacement == old:
        return
    if target in state.current:
        raise Rejected(
            "vocabulary.authoring_rejected",
            "Literal repair would collapse distinct annotation assertions",
        )
    direct = []
    for attachment in selected.attachments:
        state.operation.consume("work")
        annotated = attachment.annotated
        if (
            annotated is not None
            and _same(annotated.subject, path.statement.subject)
            and annotated.predicate == path.statement.predicate
            and _same(annotated.value, value)
        ):
            state.operation.consume("states")
            direct.append(attachment.node)
    state.operation.consume("states", len(path.attachments) + len(direct))
    proof_nodes = set(path.attachments) | set(direct)
    for statement in selected.statements:
        state.operation.consume("work")
        if (
            statement == path.root
            or statement.subject in proof_nodes
            and statement.predicate
            in (OWL.annotatedSource, OWL.annotatedProperty, OWL.annotatedTarget)
        ):
            proof = PatchQuad(
                cast(PatchResource, state.ref(statement.subject)),
                statement.predicate,
                state.ref(statement.value),
                state.graph_ref,
            )
            if proof not in state.current:
                raise PatchUnavailable(
                    "An earlier edit changed the original annotation anchor path"
                )
    state.operation.consume("states", len(selected.attachments))
    selected_refs = {
        cast(PatchResource, state.ref(attachment.node))
        for attachment in selected.attachments
    }
    affected_refs = {cast(PatchResource, state.ref(node)) for node in proof_nodes}
    for quad in state.current:
        state.operation.consume("work")
        if (
            quad.subject in affected_refs
            and quad.graph != state.graph_ref
            or quad.graph in affected_refs
        ):
            raise PatchUnavailable(
                "Cross-graph attachment ownership prevents lexical repair"
            )
        if quad.value in affected_refs and not (
            quad.graph == state.graph_ref
            and quad.predicate == OWL.annotatedSource
            and quad.subject in selected_refs
        ):
            raise PatchUnavailable(
                "Shared or unselected attachment ownership prevents lexical repair"
            )
    state.drop(owner, path.statement.predicate, old)
    state.put(owner, path.statement.predicate, replacement)
    for node in direct:
        reference = cast(PatchResource, state.ref(node))
        state.drop(reference, OWL.annotatedTarget, old)
        state.put(reference, OWL.annotatedTarget, replacement)
