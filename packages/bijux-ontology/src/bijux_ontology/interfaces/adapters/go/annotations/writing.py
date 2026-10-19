"""Loss-aware local table output and explicitly signed RDF assertion records."""

import csv
import io
import json
from collections.abc import Mapping
from threading import Event

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .checking import _expand, _registry
from .types import GoAnnotationExport, GoAnnotations

_SIGNED = Namespace("urn:bijux:ontology:go:assertion:")


def _required(
    annotations: GoAnnotations, operation: Operation, issues: IssueCollector
) -> None:
    """Require genuine parsed input without laundering an incomplete companion join."""
    if type(annotations) is not GoAnnotations:
        raise InputError("Export requires parsed GoAnnotations")
    retain_report(annotations.report, operation, issues)
    if not annotations.report.verified:
        raise ResourceError(
            "Incomplete or invalid source assertions cannot qualify export"
        )


def _failure(error: InputError, issues: IssueCollector) -> None:
    """Separate bounded stopping from incomplete or malformed export inputs."""
    if isinstance(error, LimitExceeded):
        issues.exhausted(str(error))
        return
    try:
        issues.append(
            Issue(
                "go.export_unverified"
                if isinstance(error, ResourceError)
                else "go.input_invalid",
                str(error),
                error.location,
                kind="unverified"
                if isinstance(error, ResourceError)
                else "input_error",
            )
        )
    except LimitExceeded as exhausted:
        issues.exhausted(str(exhausted))


def write_go_annotations(
    annotations: GoAnnotations,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> GoAnnotationExport:
    """Serialize the same supported format without changing fields or grouping.

    This is table transport, not GAF-to-GPAD evidence mapping. The caller retains
    the original explicit GPI resource through GoAnnotations.gpi.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    content = None
    checked = 0
    try:
        _required(annotations, operation, issues)
        rows: list[tuple[str, ...]] = []
        headers: list[str] = []
        total = 0
        for header in annotations.headers:
            bound_text(header, operation)
            operation.consume("states")
            operation.consume("bytes", 4 * len(header))
            total += len(header.encode("utf-8")) + 1
            headers.append(header)
        for annotation in annotations.annotations:
            operation.consume("states", len(annotation.fields) + 1)
            for field in annotation.fields:
                bound_text(field, operation)
                operation.consume("bytes", 4 * len(field))
                total += len(field.encode("utf-8")) + 1
            rows.append(annotation.fields)
        if total > operation.limits.max_bytes:
            raise LimitExceeded("GO table output exceeds max_bytes")
        operation.consume("bytes", 16 * total + 1024)
        output = io.StringIO()
        for header in headers:
            output.write(header + "\n")
        writer = csv.writer(
            output,
            delimiter="\t",
            quoting=csv.QUOTE_NONE,
            quotechar=None,
            lineterminator="\n",
        )
        for row in rows:
            operation.consume("work", len(row) + 1)
            writer.writerow(row)
            checked += 1
        text = output.getvalue()
        bound_text(text, operation)
        content = text
    except InputError as error:
        _failure(error, issues)
    return GoAnnotationExport(
        content,
        None,
        Report(
            "go.annotations.write",
            checked,
            tuple(issues),
            requested=max(
                1,
                len(annotations.annotations)
                if type(annotations) is GoAnnotations
                else 1,
            ),
            complete=content is not None,
            regime="same-format-lossless-table-transport",
        ),
    )


def annotations_to_rdf(
    annotations: GoAnnotations,
    registry: IdentifierRegistry,
    *,
    relations: Mapping[str, str] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> GoAnnotationExport:
    """Export signed reified records; never emit positive edges or OWL negatives.

    RDF reification describes an annotation without asserting its quoted triple.
    Source fields, evidence grouping and companion metadata are retained in a
    typed JSON payload. No equivalence to an OWL interpretation or a GO-CAM
    association model is claimed.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    result = None
    checked = 0
    try:
        _required(annotations, operation, issues)
        prefixes, labels = _registry(registry, relations, operation)
        graph = Graph()
        for annotation in annotations.annotations:
            operation.consume("states", 24 + len(annotation.fields))
            operation.consume("bytes", 2048)
            bound_text(annotation.source, operation)
            for field in annotation.fields:
                bound_text(field, operation)
            subject = _expand(
                annotation.subject_form or annotation.subject, prefixes, operation
            )
            relation = (
                labels.get(annotation.relation)
                if annotation.format == "gaf"
                else annotation.relation
            )
            if relation is None:
                raise ResourceError(
                    "RDF export requires an explicit GAF relation mapping"
                )
            predicate = _expand(relation, prefixes, operation)
            term = _expand(annotation.term, prefixes, operation)
            payload: dict[str, object] = {
                "format": annotation.format,
                "source": annotation.source,
                "line": annotation.line,
                "fields": annotation.fields,
            }
            if annotation.entity is not None:
                for field in annotation.entity.fields:
                    bound_text(field, operation)
                payload["gpi"] = {
                    "source": annotation.entity.source,
                    "line": annotation.entity.line,
                    "fields": annotation.entity.fields,
                }
            size = json_bound(payload, operation)
            if size > operation.limits.max_bytes:
                raise LimitExceeded("GO assertion payload exceeds max_bytes")
            operation.consume("bytes", 2 * size + 128)
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            node = BNode()
            triples = (
                (node, RDF.type, RDF.Statement),
                (node, RDF.type, _SIGNED.Record),
                (node, RDF.subject, URIRef(subject)),
                (node, RDF.predicate, URIRef(predicate)),
                (node, RDF.object, URIRef(term)),
                (
                    node,
                    _SIGNED.negated,
                    Literal(annotation.negated, datatype=XSD.boolean),
                ),
                (
                    node,
                    _SIGNED.payload,
                    Literal(encoded, datatype=RDF.JSON, normalize=False),
                ),
            )
            for triple in triples:
                operation.consume("work")
                operation.consume("triples")
                graph.add(triple)
            checked += 1
        result = prepare_source(graph, budget=operation)
    except InputError as error:
        _failure(error, issues)
    return GoAnnotationExport(
        None,
        result,
        Report(
            "go.annotations.rdf",
            checked,
            tuple(issues),
            requested=max(
                1,
                len(annotations.annotations)
                if type(annotations) is GoAnnotations
                else 1,
            ),
            complete=result is not None,
            regime="signed-reified-annotation-records-no-OWL-interpretation",
        ),
    )
