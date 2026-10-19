"""Explicit fresh label-resource identities for selected plain SKOS assertions."""

import threading
from collections.abc import Sequence

from rdflib import BNode, URIRef
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches.types import (
    PatchLiteral,
    PatchQuad,
    PatchUnavailable,
    PatchValue,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text, literal_datatype
from bijux_ontology.reports import Report
from bijux_ontology.skos.xl import XL

from .execution import capture, finish, integrity, unavailable
from .types import LabelCandidate, LabelCorrespondence, LabelIdentity

_ROLES = {
    SKOS.prefLabel: XL.prefLabel,
    SKOS.altLabel: XL.altLabel,
    SKOS.hiddenLabel: XL.hiddenLabel,
}


def promote_labels(
    source: PreparedSource,
    identities: Sequence[LabelIdentity],
    *,
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> LabelCandidate:
    """Add XL link/type/form assertions using exact caller-assigned fresh IRIs.

    Plain assertions remain. No shared identity is inferred from equal forms or
    from an existing label resource. Every supplied assignment must select an
    actual original assertion; duplicate assignments, reused resources and any
    existing occurrence of a requested IRI leave the candidate unavailable.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    correspondence: list[LabelCorrespondence] = []
    captured = None
    output: set[PatchQuad] | None = None
    try:
        captured = capture(source, operation)
        selected = capture_items(identities, LabelIdentity, operation)
        if not selected:
            raise PatchUnavailable(
                "Promotion requires explicit label identity assignments"
            )
        operation.consume("states")
        children.append(integrity(source, operation, issues))
        operation.consume("states", 7 * len(captured.witness.quads))
        original = set(captured.witness.quads)
        resources: set[PatchValue | None] = set()
        for quad in captured.witness.quads:
            operation.consume("work")
            resources.update((quad.subject, quad.predicate, quad.value, quad.graph))
            if isinstance(quad.value, PatchLiteral):
                bound_text(quad.value.datatype, operation)
                resources.add(URIRef(quad.value.datatype))
        used = set()
        assertions = set()
        output = set(original)
        for identity in selected:
            operation.consume("work")
            for node in (
                identity.owner,
                identity.predicate,
                identity.value,
                identity.resource,
            ):
                bound_node(node, operation)
            if identity.predicate not in _ROLES:
                raise PatchUnavailable(
                    "Promotion requires a standard plain SKOS label role"
                )
            if identity.resource in resources or identity.resource in used:
                raise PatchUnavailable(
                    "Every promoted label needs a distinct unused IRI"
                )
            owner = (
                captured.reference(identity.owner)
                if isinstance(identity.owner, BNode)
                else URIRef(identity.owner)
            )
            value = PatchLiteral(
                str(identity.value),
                str(literal_datatype(identity.value)),
                identity.value.language,
            )
            if value.datatype not in (str(XSD.string), str(RDF.langString)):
                raise PatchUnavailable("Only lexical string labels can be promoted")
            plain = PatchQuad(
                owner, identity.predicate, value, captured.witness.graph_selection
            )
            if plain not in original or plain in assertions:
                raise PatchUnavailable(
                    "Each identity must select a distinct original assertion"
                )
            operation.consume("states", 7)
            used.add(identity.resource)
            assertions.add(plain)
            link = PatchQuad(
                owner, _ROLES[identity.predicate], identity.resource, plain.graph
            )
            form = PatchQuad(identity.resource, XL.literalForm, value, plain.graph)
            output.update(
                (
                    link,
                    form,
                    PatchQuad(identity.resource, RDF.type, XL.Label, plain.graph),
                )
            )
            correspondence.append(
                LabelCorrespondence(identity.resource, link, form, plain)
            )
    except LimitExceeded as error:
        output = None
        issues.exhausted(str(error))
    except PatchUnavailable as error:
        output = None
        unavailable(issues, error)
    return finish(
        source,
        captured,
        output,
        correspondence,
        [],
        issues,
        children,
        operation,
        after_checks,
    )
