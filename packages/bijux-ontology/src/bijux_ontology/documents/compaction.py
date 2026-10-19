"""Context-controlled JSON-LD exports verified against their selected RDF input."""

import json

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import own_json
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import (
    DocumentLoader,
    _consume_work,
    _json_size,
    context_resources,
)
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource

from .export import RDFExport, export_rdf
from .preparation import prepare_source


def export_jsonld(
    source: PreparedSource,
    *,
    context: object,
    frame: dict[str, object] | None = None,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    preserve_graphs: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> RDFExport:
    """Compact through PyLD, then re-expand and compare the selected RDF scope.

    The context is its JSON-LD value, such as a mapping, URL or context array.
    Resources are offline by default; base is the logical output document base.
    Exact supported RDF terms and named graph membership must round-trip before
    bytes become writable. Empty named graphs use export_rdf's explicit loss or
    exclusion policy. RDF cannot recover source JSON shape, index annotations or
    other non-RDF metadata, and this operation makes no preservation claim for
    them. The output includes the supplied context and may use relative IRIs.
    An optional standard frame can request reverse fields or embedding; a frame
    selecting away required RDF blocks export like any other semantic loss.
    """
    if base is not None:
        absolute_iri(base)
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    reports: list[Report] = []
    try:
        owned_context = own_json(
            context,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        operation.consume("bytes", _json_size(owned_context))
        _consume_work(owned_context, operation)
        owned_frame: dict[str, object] | None = None
        if frame is not None:
            frame_snapshot = own_json(
                frame,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
            )
            if not isinstance(frame_snapshot, dict):
                raise InputError("An export frame must be a JSON object")
            owned_frame = frame_snapshot
            if "@context" in owned_frame and owned_frame["@context"] != owned_context:
                raise InputError("The export frame conflicts with the supplied context")
            operation.consume("bytes", _json_size(owned_frame))
            _consume_work(owned_frame, operation)
        intermediate = export_rdf(
            source,
            format="nquads",
            graph_name=graph_name,
            union=union,
            preserve_graphs=preserve_graphs,
            budget=operation,
            timeout_seconds=timeout_seconds,
        )
        reports.append(intermediate.report)
        if intermediate.content is None:
            return RDFExport(
                None, combine_reports("jsonld.export", *reports), "json-ld"
            )
        jsonld = processor()
        try:
            expanded: object = jsonld.from_rdf(
                intermediate.content.decode("utf-8"),
                options={"format": "application/n-quads", "useNativeTypes": False},
            )
            _consume_work(expanded, operation)
            operation.consume("bytes", _json_size(expanded))
            options = {
                "base": base,
                "documentLoader": resources,
                "processingMode": "json-ld-1.1",
                "contextResolver": jsonld.ContextResolver(
                    {}, resources, max_context_urls=operation.limits.max_resources
                ),
            }
            candidate: object = (
                jsonld.compact(expanded, owned_context, options=options)
                if owned_frame is None
                else jsonld.frame(
                    expanded,
                    {**owned_frame, "@context": owned_context},
                    options=options,
                )
            )
        except jsonld.JsonLdError as error:
            raise InputError(str(error)) from error
        candidate = own_json(
            candidate,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        _consume_work(candidate, operation)
        content = json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        operation.consume("bytes", len(content))
        after = prepare_jsonld(candidate, base=base, document_loader=resources)
        reports.append(after.report)
        if after.source is not None and after.report.verified:
            expected = prepare_source(
                intermediate.content, format="nquads", budget=operation
            )
            reports.append(
                compare_rdf(
                    expected,
                    after.source,
                    budget=operation,
                    timeout_seconds=timeout_seconds,
                )
            )
        report = combine_reports("jsonld.export", *reports)
        accepted = report.complete and all(
            issue.kind == "finding" and issue.severity == "info"
            for issue in report.issues
        )
        return RDFExport(content if accepted else None, report, "json-ld")
    except (LimitExceeded, RecursionError, OverflowError) as error:
        issue = Issue("execution.limit_exceeded", str(error), kind="exhausted")
    except InputError as error:
        issue = Issue("context.input_error", str(error), kind="input_error")
    reports.append(
        Report("jsonld.export", issues=(issue,), requested=1, complete=False)
    )
    return RDFExport(None, combine_reports("jsonld.export", *reports), "json-ld")
