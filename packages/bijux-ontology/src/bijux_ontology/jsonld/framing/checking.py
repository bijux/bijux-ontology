"""Finite retained-record projection checks over maintained framing output."""

import json
import math
from collections.abc import Sequence

from rdflib import BNode, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.evidence.entities import EntityKey
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import (
    DocumentLoader,
    _json_size,
    context_resources,
)
from bijux_ontology.jsonld.selectors.checking import resolve_selection
from bijux_ontology.jsonld.selectors.types import EntitySelector
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.rdf.transforms import extract_neighbourhood

from .processing import own_document, process_frame
from .types import FramedDocument, FrameNode, FrameOmission


def _predicates(values: tuple[str, ...], budget: Budget) -> frozenset[str]:
    """Own finite named RDF predicates before hashing or policy comparison."""
    if type(values) is not tuple:
        raise InputError("Framing predicate scope requires immutable IRI tuples")
    budget.consume("states", len(values))
    budget.consume("bytes", 64 + 16 * len(values))
    result = set()
    for value in values:
        if not isinstance(value, str) or isinstance(value, BNode | Literal):
            raise InputError("Framing predicates require named IRI strings")
        bound_text(value, budget)
        absolute_iri(value)
        result.add(str(value))
    return frozenset(result)


def _records(
    view: PreparedUsages,
    selector: EntitySelector,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
) -> tuple[EntityKey, ...]:
    """Retain actual quantified selection truth before adopting matched identities."""
    if (
        selector.coverage_unit != "rdf_entity"
        or selector.steps
        or selector.include_references
    ):
        raise InputError("Framing roots require describing RDF entity selection")
    selection = view.select(selector)
    retain_report(selection.report, budget, issues)
    budget.consume("states")
    children.append(selection.report)
    if selection.report.outcome == "exhausted":
        raise LimitExceeded("Framing selection exceeded the operation limits")
    return selection.entities


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
    requested = 1
    output_base = None

    def unknown(message: str) -> None:
        """Describe unavailable scope without retaining arbitrary source metadata."""
        issues.append(Issue("context.frame_unverified", message, kind="unverified"))

    def adopt(report: Report) -> None:
        """Apply this operation's limits before required child adoption."""
        retain_report(report, operation, issues)
        operation.consume("states")
        children.append(report)

    def omission(value: FrameOmission) -> None:
        """Expose broad RDF exclusions without manufacturing source pointers."""
        texts = (value.identifier, value.graph or "", *(value.predicates or ()))
        estimated = 256 + 6 * sum(len(text) for text in texts)
        if estimated > operation.limits.max_bytes:
            raise LimitExceeded("Framing exclusion metadata exceeds max_bytes")
        operation.consume("work", estimated)
        operation.consume("bytes", estimated)
        operation.consume("states")
        encoded = json.dumps(
            {
                "record": value.identifier,
                "graph": value.graph,
                "predicates": value.predicates,
            },
            separators=(",", ":"),
        )
        exclusions.append(value)
        excluded_scopes.append(encoded)

    def report() -> Report:
        """State projection scope explicitly instead of whole-dataset equality."""
        return Report(
            "jsonld.framing",
            checked,
            tuple(issues),
            requested=requested,
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
        selected_predicates = _predicates(predicates, operation)
        permitted_predicates = _predicates(omitted_predicates, operation)
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
        selected = _records(before_view, retained, operation, issues, children)
        requested = max(1, len(selected))
        names = []
        selected_keys = set()
        for key in selected:
            operation.consume("work")
            operation.consume("states")
            if (
                key.identifier is None
                or key.identifier.startswith("_:")
                or key.anonymous_graph
            ):
                unknown("Retained roots need named subject and graph identity")
                continue
            bound_text(key.identifier, operation)
            names.append(key.identifier)
            selected_keys.add(key)
        if not names:
            unknown("No required named record was established for retained framing")
        permitted_keys = set()
        for selector in omission_selectors:
            matches = _records(before_view, selector, operation, issues, children)
            if selector.graph != retained.graph:
                raise InputError(
                    "Framing record allowances must use the selected physical graph"
                )
            for key in matches:
                operation.consume("work")
                operation.consume("states")
                if (
                    key.identifier is None
                    or key.identifier.startswith("_:")
                    or key.anonymous_graph
                ):
                    unknown("Record omission needs established named identity")
                    continue
                bound_text(key.identifier, operation)
                if key in selected_keys:
                    raise InputError("A required framing record cannot also be omitted")
                permitted_keys.add(key)
                omission(FrameOmission(key.identifier, key.graph))
        for entity in before_view.entities.entities:
            operation.consume("work")
            key = entity.key
            if (
                entity.described
                and not key.anonymous_graph
                and key.graph == retained.graph
                and key.identifier is not None
                and not key.identifier.startswith("_:")
            ):
                bound_text(key.identifier, operation)
                if key not in selected_keys and key not in permitted_keys:
                    unknown("A source record is outside the retained/omitted inventory")
        before = prepare_jsonld(
            original, base=base, document_loader=resources, direction=direction
        )
        adopt(before.report)
        if before.source is None or not before.report.verified or not names:
            return FramedDocument(
                None, output_base, None, (), tuple(exclusions), report()
            )
        graph_name = retained.graph if retained.graph is not None else "@default"
        before_graph = before.source._read(graph_name, budget=operation)
        exercised_permissions = set()
        for name in names:
            retained_count = 0
            excluded_predicates = set()
            for _, predicate, value in before_graph.triples((URIRef(name), None, None)):
                operation.consume("work")
                bound_node(predicate, operation)
                bound_node(value, operation)
                if str(predicate) in selected_predicates:
                    retained_count += 1
                elif str(predicate) in permitted_predicates:
                    operation.consume("states")
                    excluded_predicates.add(str(predicate))
                    exercised_permissions.add(str(predicate))
                else:
                    unknown(
                        "A source seed predicate has no retention or omission policy"
                    )
            if not retained_count:
                unknown(
                    "A required named record supplies no selected retained assertion"
                )
            if excluded_predicates:
                operation.consume("work", len(omitted_predicates))
                operation.consume("bytes", 16 * len(excluded_predicates))
                omission(
                    FrameOmission(
                        name,
                        retained.graph,
                        tuple(
                            value
                            for value in omitted_predicates
                            if value in excluded_predicates
                        ),
                    )
                )
        if exercised_permissions != permitted_predicates:
            unknown("A predicate omission has no observed retained-seed scope")
        candidate = process_frame(original, owned_frame, resources, output_base)
        after = prepare_jsonld(
            candidate, base=output_base, document_loader=resources, direction=direction
        )
        adopt(after.report)
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
        established_after = _records(
            after_view, after_selected, operation, issues, children
        )
        if len(established_after) != len(names):
            checked = len(names)
            return FramedDocument(
                None, output_base, None, (), tuple(exclusions), report()
            )
        for entity in after_view.entities.entities:
            operation.consume("work")
            key = entity.key
            if (
                entity.described
                and not key.anonymous_graph
                and key.graph == retained.graph
                and key.identifier is not None
                and not key.identifier.startswith("_:")
            ):
                bound_text(key.identifier, operation)
                if key not in selected_keys and key not in permitted_keys:
                    unknown("An output description is outside the projection inventory")
        after_graph = after.source._read(graph_name, budget=operation)
        for name in names:
            for _, predicate, value in after_graph.triples((URIRef(name), None, None)):
                operation.consume("work")
                bound_node(predicate, operation)
                bound_node(value, operation)
                if (
                    str(predicate) not in selected_predicates
                    and str(predicate) not in permitted_predicates
                ):
                    unknown("An output seed predicate is outside the projection scope")
        extracted = []
        for source in (before.source, after.source):
            selected_source = extract_neighbourhood(
                source,
                tuple(names),
                predicates=tuple(selected_predicates),
                graph_name=graph_name,
                budget=operation,
            )
            adopt(selected_source.report)
            if selected_source.source is not None and selected_source.report.verified:
                extracted.append(selected_source.source)
        if len(extracted) == 2:
            comparison = compare_rdf(
                extracted[0],
                extracted[1],
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
            adopt(comparison)
            if comparison.complete:
                checked = len(names)
        nodes = []
        for name in names:
            paths = resolve_selection(
                EntitySelector(
                    identifiers=(name,),
                    graph=retained.graph,
                    coverage_unit="source_occurrence",
                    minimum=1,
                ),
                candidate,
                after_view.entities,
                after_view.observe,
                operation,
                require_usage=False,
                association_only=True,
            )
            adopt(paths.report)
            operation.consume("states")
            operation.consume("bytes", 96 + 16 * len(paths.paths))
            nodes.append(FrameNode(name, retained.graph, paths.paths))
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
            adopt(failure)
        except LimitExceeded:
            issues.exhausted("Framing failure metadata exceeded the operation limits")
    return FramedDocument(None, output_base, None, (), tuple(exclusions), report())
