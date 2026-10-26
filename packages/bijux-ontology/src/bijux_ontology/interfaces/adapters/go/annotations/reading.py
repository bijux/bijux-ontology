"""Compose versioned GO row admission, annotation meaning and companion coverage."""

from threading import Event
from typing import cast

from bijux_ontology.diagnostics.errors import (
    InputError,
    LimitExceeded,
    ResourceError,
)
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)

from .assertions import interpret_annotation, interpret_gene_product
from .companions import join_gene_products
from .rows import decode_rows
from .syntax import source_error
from .types import (
    GoAnnotation,
    GoAnnotationDocument,
    GoAnnotations,
    GoGeneProduct,
    GoGeneProductDocument,
)


def read_go_annotations(
    document: GoAnnotationDocument,
    *,
    gpi: GoGeneProductDocument | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> GoAnnotations:
    """Read supplied current formats without fetching or repairing identifiers."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    annotations: list[GoAnnotation] = []
    entities: list[GoGeneProduct] = []
    headers: tuple[str, ...] = ()
    children: list[Report] = []
    join = None
    requested = 1
    complete = True
    try:
        if type(document) is not GoAnnotationDocument:
            raise InputError("Supply a typed GAF/GPAD document")
        declared_format = cast(object, document.format)
        if type(declared_format) is not str or document.format not in (
            "gaf",
            "gpad",
        ):
            raise InputError("Supply a typed GAF/GPAD document and explicit format")
        if gpi is not None and type(gpi) is not GoGeneProductDocument:
            raise InputError("Supply a typed explicit GPI companion resource")
        headers, rows, complete = decode_rows(
            document.source, document.content, document.format, operation, issues
        )
        requested = max(1, len(rows))
        for number, fields in rows:
            try:
                operation.consume("states", 8)
                operation.consume("bytes", 1024)
                annotations.append(
                    interpret_annotation(
                        document.source, number, fields, document.format, operation
                    )
                )
            except LimitExceeded:
                raise
            except InputError as error:
                complete = False
                located = (
                    error
                    if error.failure is not None
                    else source_error(str(error), document.source, number)
                )
                issues.append(
                    Issue(
                        "go.input_invalid",
                        str(error),
                        document.source,
                        kind="input_error",
                        details=Location.from_error(located),
                    )
                )
        if document.format == "gpad" and gpi is None:
            raise ResourceError("GPAD requires its explicitly supplied GPI companion")
        if gpi is not None:
            _, entity_rows, entity_complete = decode_rows(
                gpi.source, gpi.content, "gpi", operation, issues
            )
            for number, fields in entity_rows:
                try:
                    entities.append(
                        interpret_gene_product(gpi.source, number, fields, operation)
                    )
                except LimitExceeded:
                    raise
                except InputError as error:
                    entity_complete = False
                    located = source_error(str(error), gpi.source, number)
                    issues.append(
                        Issue(
                            "go.input_invalid",
                            str(error),
                            gpi.source,
                            kind="input_error",
                            details=Location.from_error(located),
                        )
                    )
            annotations, join = join_gene_products(
                annotations, entities, entity_complete, complete, operation
            )
            children.append(join.report)
            retain_report(join.report, operation, issues)
            complete = complete and entity_complete and join.report.complete
            for resolution in join.resolutions:
                if resolution.status == "resolved":
                    continue
                assertion = annotations[int(resolution.source.identifier)]
                located = source_error(
                    "Required companion subject is " + resolution.status,
                    assertion.source,
                    assertion.line,
                )
                issues.append(
                    Issue(
                        "go.gpi_subject_" + resolution.status,
                        "Required companion subject is " + resolution.status,
                        assertion.source,
                        kind="unverified"
                        if resolution.status == "unverified"
                        else "finding",
                        actual=assertion.subject,
                        details=Location.from_error(located),
                    )
                )
        if not annotations:
            complete = False
            issues.append(
                Issue(
                    "go.required_annotations_empty",
                    "No required annotation assertions were established",
                    document.source,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        complete = False
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        complete = False
        try:
            issues.append(
                Issue(
                    "go.input_unverified"
                    if isinstance(error, ResourceError)
                    else "go.input_invalid",
                    str(error),
                    error.location,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return GoAnnotations._owned(
        source=document,
        gpi=gpi,
        headers=headers,
        annotations=tuple(annotations),
        gene_products=tuple(entities),
        join=join,
        report=Report(
            "go.annotations.read",
            len(annotations),
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
            regime="explicit-current-GO-annotation-formats",
        ),
    )
