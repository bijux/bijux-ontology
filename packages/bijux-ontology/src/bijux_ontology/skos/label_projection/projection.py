"""Standard SKOS-XL link/form consequences in an owned plain-label projection."""

import threading
from collections.abc import Sequence

from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.patches.types import (
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report
from bijux_ontology.skos.xl import XL

from .execution import capture, finish, integrity, unavailable
from .types import LabelCandidate, LabelCorrespondence

_ROLES = {
    XL.prefLabel: SKOS.prefLabel,
    XL.altLabel: SKOS.altLabel,
    XL.hiddenLabel: SKOS.hiddenLabel,
}


def project_xl_labels(
    source: PreparedSource,
    *,
    after_checks: Sequence[MigrationCheck] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> LabelCandidate:
    """Project asserted XL roles through exactly one lexical literal form.

    Original label resources remain untouched. The candidate removes statements
    incident on recognized XL label resources and replaces their role links with
    plain consequences. Every removed statement is returned in ``omitted``;
    identities, metadata and label relations cannot be reconstructed from plain
    labels. Unaffected assertions remain. This is a deliberate representation
    projection, not an RDF-equivalent or lossless round-trip transformation.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    correspondence: list[LabelCorrespondence] = []
    omitted: list[PatchQuad] = []
    captured = None
    output: set[PatchQuad] | None = None
    try:
        captured = capture(source, operation)
        operation.consume("states")
        children.append(integrity(source, operation, issues))
        labels: set[PatchResource] = set()
        forms: dict[PatchResource, list[PatchQuad]] = {}
        links = []
        for quad in captured.witness.quads:
            operation.consume("work")
            if quad.predicate in _ROLES:
                if isinstance(quad.value, PatchLiteral):
                    raise PatchUnavailable("An XL label link needs a resource endpoint")
                operation.consume("states", 2)
                labels.add(quad.value)
                links.append(quad)
            if quad.predicate == XL.literalForm:
                operation.consume("states", 2)
                labels.add(quad.subject)
                forms.setdefault(quad.subject, []).append(quad)
            if quad.predicate == RDF.type and quad.value == XL.Label:
                operation.consume("states")
                labels.add(quad.subject)
            if quad.predicate == XL.labelRelation:
                if isinstance(quad.value, PatchLiteral):
                    raise PatchUnavailable(
                        "An XL label relation needs resource endpoints"
                    )
                operation.consume("states", 2)
                labels.update((quad.subject, quad.value))
        if not links:
            raise PatchUnavailable("No asserted XL role links exercise this projection")
        for label in labels:
            operation.consume("work")
            values = forms.get(label, [])
            if len(values) != 1 or not isinstance(values[0].value, PatchLiteral):
                raise PatchUnavailable(
                    "Each selected XL resource needs exactly one form"
                )
            value = values[0].value
            if value.datatype not in (str(XSD.string), str(RDF.langString)):
                raise PatchUnavailable("XL literal forms require lexical string terms")
        operation.consume("states", len(captured.witness.quads))
        output = set()
        for quad in captured.witness.quads:
            operation.consume("work")
            if (
                quad.subject in labels
                or quad.value in labels
                or quad.predicate in labels
            ):
                operation.consume("states")
                omitted.append(quad)
            else:
                output.add(quad)
        for link in links:
            operation.consume("work")
            assert not isinstance(link.value, PatchLiteral)
            form = forms[link.value][0]
            operation.consume("states", 3)
            plain = PatchQuad(
                link.subject, _ROLES[link.predicate], form.value, link.graph
            )
            correspondence.append(LabelCorrespondence(link.value, link, form, plain))
            output.add(plain)
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
        omitted,
        issues,
        children,
        operation,
        after_checks,
    )
