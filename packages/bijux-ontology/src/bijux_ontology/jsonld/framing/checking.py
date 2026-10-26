"""Finite retained-record projection checks over maintained framing output."""

import json
import math
from collections.abc import Sequence

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
)
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import (
    DocumentLoader,
    _json_size,
    context_resources,
)
from bijux_ontology.jsonld.selectors.checking import resolve_entity_associations
from bijux_ontology.jsonld.selectors.types import EntitySelector
from bijux_ontology.jsonld.usages.checking import prepare_usages
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.transforms import extract_neighbourhood

from .preservation import (
    FramingPreservation,
    adopt_report,
    capture_predicates,
    select_records,
)
from .processing import own_document, process_frame
from .types import FramedDocument, FrameNode, FrameOmission


def frame_document(
    document: object,
    frame: object,
    *,
    retained: EntitySelector,
    predicates: tuple[str, ...],
    omitted: Sequence[EntitySelector] = (),
    omitted_predicates: tuple[str, ...] = (),
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> FramedDocument:
    """Frame original input and qualify an explicit named-record RDF projection.

    Each selected seed preserves its declared predicates and full attached blank
    closure, jointly across roots. Named child descriptions require selection.
    Omitted seed predicates are explicitly outside preservation truth, not just
    permitted deletions; framing defaults there are also outside the comparison.
    Each omission needs observed source scope, without invented source pointers.
    Ordinary processor execution remains in process.
    """
    if not isinstance(retained, EntitySelector):
        raise InputError("Framing requires an explicit retained entity selector")
    if (
        type(timeout_seconds) not in (int, float)
        or timeout_seconds <= 0
        or isinstance(timeout_seconds, float)
        and not math.isfinite(timeout_seconds)
        or isinstance(timeout_seconds, int)
        and timeout_seconds.bit_length() > 53
    ):
        raise InputError("Framing comparison timeout must be finite and positive")
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    issues = IssueCollector(operation)
    children: list[Report] = []
    exclusions: list[FrameOmission] = []
    excluded_scopes: list[str] = []
    checked = 0
    plan: FramingPreservation | None = None
    output_base = None

    def report() -> Report:
        """State projection scope explicitly instead of whole-dataset equality."""
        return Report(
            "jsonld.framing",
            checked,
            tuple(issues),
            requested=plan.requested if plan is not None else 1,
            allow_empty=True,
            exclusions=tuple(excluded_scopes),
            children=tuple(children),
            regime="selected-jsonld-projection",
            families=("projection.retention", "projection.output_paths"),
        )

    try:
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
            output_base = base
        selected_predicates = capture_predicates(predicates, operation)
        permitted_predicates = capture_predicates(omitted_predicates, operation)
        if not selected_predicates or selected_predicates & permitted_predicates:
            raise InputError(
                "Required predicates must be nonempty and disjoint from omissions"
            )
        omission_selectors = capture_items(omitted, EntitySelector, operation)
        original = own_document(document, resources)
        owned_frame = own_document(frame, resources)
        before_view = prepare_usages(original, base=base, document_loader=resources)
        if base is None and isinstance(original, str):
            output_base = resources.effective_url(original)
            if output_base is None:
                raise InputError(
                    "Framing input has no established effective document URL"
                )
            bound_text(output_base, operation)
            absolute_iri(output_base)
        plan = FramingPreservation(
            retained,
            selected_predicates,
            permitted_predicates,
            omitted_predicates,
            operation,
            issues,
            children,
            exclusions,
            excluded_scopes,
        )
        plan.capture(before_view, omission_selectors)
        names = plan.names
        before = prepare_jsonld(
            original, base=base, document_loader=resources, direction=direction
        )
        adopt_report(before.report, operation, issues, children)
        if before.source is None or not before.report.verified or not names:
            return FramedDocument(
                None, output_base, None, (), tuple(exclusions), report()
            )
        graph_name = plan.graph_name
        plan.qualify_source(before.source)
        candidate = process_frame(original, owned_frame, resources, output_base)
        after = prepare_jsonld(
            candidate, base=output_base, document_loader=resources, direction=direction
        )
        adopt_report(after.report, operation, issues, children)
        if after.source is None or not after.report.verified:
            return FramedDocument(
                None, output_base, None, (), tuple(exclusions), report()
            )
        after_view = prepare_usages(
            candidate, base=output_base, document_loader=resources
        )
        after_selected = EntitySelector(
            identifiers=tuple(names),
            graph=retained.graph,
            minimum=len(names),
            maximum=len(names),
        )
        established_after = select_records(
            after_view, after_selected, operation, issues, children
        )
        if len(established_after) != len(names):
            checked = len(names)
            return FramedDocument(
                None, output_base, None, (), tuple(exclusions), report()
            )
        plan.qualify_output(after_view, after.source)
        extracted = []
        for source in (before.source, after.source):
            selected_source = extract_neighbourhood(
                source,
                tuple(names),
                predicates=tuple(selected_predicates),
                graph_name=graph_name,
                budget=operation,
            )
            adopt_report(selected_source.report, operation, issues, children)
            if selected_source.source is not None and selected_source.report.verified:
                extracted.append(selected_source.source)
        if len(extracted) == 2:
            comparison = compare_rdf(
                extracted[0],
                extracted[1],
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
            adopt_report(comparison, operation, issues, children)
            if comparison.complete:
                checked = len(names)
        operation.consume("states", len(names))
        operation.reserve_storage(128 * len(names))
        associations = resolve_entity_associations(
            tuple(
                EntitySelector(
                    identifiers=(name,),
                    graph=retained.graph,
                    coverage_unit="source_occurrence",
                    minimum=1,
                )
                for name in names
            ),
            candidate,
            after_view.entities,
            after_view.observe,
            operation,
        )
        nodes = []
        try:
            for name, paths in zip(names, associations, strict=True):
                adopt_report(paths.report, operation, issues, children)
                operation.consume("states")
                operation.consume("bytes", 96 + 16 * len(paths.paths))
                nodes.append(FrameNode(name, retained.graph, paths.paths))
        except LimitExceeded:
            for selection in associations:
                issues.retain_completed(selection.report)
            raise
        result = report()
        if result.verified:
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            operation.consume("bytes", _json_size(candidate))
            return FramedDocument(
                encoded,
                output_base,
                after.source,
                tuple(nodes),
                tuple(exclusions),
                result,
            )
    except (LimitExceeded, RecursionError, OverflowError):
        issues.exhausted("JSON-LD framing exceeded the operation limits")
    except ResourceError:
        raise
    except InputError as error:
        try:
            failure = Report(
                "jsonld.frame_input",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "context.frame_input_error",
                        "Invalid framing input or scope",
                        kind="input_error",
                        details=Location.from_error(error),
                    ),
                ),
            )
            adopt_report(failure, operation, issues, children)
        except LimitExceeded:
            issues.exhausted("Framing failure metadata exceeded the operation limits")
    return FramedDocument(None, output_base, None, (), tuple(exclusions), report())
