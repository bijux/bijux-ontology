"""Finite public operation dispatch; no executable callbacks cross the protocol."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Literal, cast

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.codecs import models_from_data, terms_from_data
from bijux_ontology.diagnostics.encoding import report_to_data
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import (
    array_value,
    boolean_value,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.jsonld.contexts.comparison import compare_context_definitions
from bijux_ontology.jsonld.contexts.processing import inspect_context
from bijux_ontology.jsonld.contexts.types import ContextScope
from bijux_ontology.jsonld.contracts.codecs import expectations_from_data
from bijux_ontology.jsonld.flattening import flatten_document
from bijux_ontology.jsonld.framing.checking import frame_document
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.jsonld.selectors.codecs import selector_from_data
from bijux_ontology.jsonld.selectors.types import EntitySelector
from bijux_ontology.jsonld.usages.checking import check_context, prepare_usages
from bijux_ontology.jsonld.usages.comparison import compare_serializations
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.codecs import usages_from_data
from bijux_ontology.models.emission.observation_codecs import bindings_from_data
from bijux_ontology.models.emission.serialization import check_serialized
from bijux_ontology.models.persistence.codecs import declaration_data
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.projects.checking import (
    _check_project_owned,
    check_project,
)
from bijux_ontology.workflows.projects.configuration import ProjectConfig

OPERATIONS = frozenset(
    (
        "context",
        "context_inspection",
        "context_definitions",
        "emission",
        "serialization",
        "flatten",
        "framing",
        "vocabulary",
        "project",
        "selection",
    )
)


def _context_scopes(value: object, limits: Limits) -> tuple[ContextScope, ...]:
    """Decode only a finite list of explicit scoped-context transitions."""
    if not isinstance(value, list):
        raise InputError("Scoped context selections require an array")
    if len(value) > limits.max_states:
        raise LimitExceeded("Scoped context selections exceed max_states")
    selected = []
    for item in value:
        scope = object_value(item, location="worker context scope")
        known_keys(scope, {"term", "kind"}, location="worker context scope")
        selected.append(
            ContextScope(
                string_value(scope.get("term")),
                cast(
                    Literal["property", "type"],
                    string_value(scope.get("kind", "property")),
                ),
            )
        )
    return tuple(selected)


def _frame_selector(value: object, limits: Limits) -> EntitySelector:
    """Capture only finite existing entity selectors for framed-record scope."""
    data = object_value(value, location="worker framing selector")
    for key in ("identifiers", "types", "steps"):
        values = array_value(data.get(key, []))
        if len(values) > limits.max_states:
            raise LimitExceeded("Framing selector exceeds max_states")
    selector = selector_from_data(data)
    if not isinstance(selector, EntitySelector):
        raise InputError("Framing requires entity selectors")
    return selector


def _frame_strings(value: object, limits: Limits) -> tuple[str, ...]:
    """Bound protocol arrays before constructing owned predicate tuples."""
    values = array_value(value)
    if len(values) > limits.max_states:
        raise LimitExceeded("Framing predicate scope exceeds max_states")
    return tuple(string_value(item) for item in values)


def execute_operation(
    operation: str,
    parameters: dict[str, object],
    limits: Limits,
    completed: Callable[[Report], None],
    *,
    job_deadline: Callable[[float | None], None] | None = None,
) -> tuple[Report, object]:
    """Execute one supported public contract using owned resources and limits."""
    if operation not in OPERATIONS:
        raise InputError("Unsupported isolated operation", location=operation)
    if operation == "project":
        known_keys(
            parameters,
            {"configuration", "directory"},
            location="worker request",
        )
        config = object_value(
            parameters.get("configuration"),
            location="worker request",
        )
        config = {**config, "limits": vars(limits)}
        project = ProjectConfig(
            config, directory=Path(string_value(parameters.get("directory")))
        )
        if job_deadline is None:
            return check_project(project, completed=completed), None
        return _check_project_owned(
            project, completed=completed, job_deadline=job_deadline
        ), None
    if operation == "vocabulary":
        known_keys(
            parameters,
            {"source", "format", "base", "terms", "graph_name", "union"},
            location="worker request",
        )
        source = RdfSource(
            string_value(parameters.get("source")),
            format=optional_string_value(parameters.get("format")),
            document_iri=optional_string_value(parameters.get("base")),
            graph_name=optional_string_value(parameters.get("graph_name")),
            union=boolean_value(parameters.get("union", False)),
        )
        allowance = Budget(limits)
        vocabulary = load_vocabulary(source, budget=allowance)
        return check_vocabulary(
            vocabulary,
            terms_from_data(parameters.get("terms"), budget=allowance),
            budget=allowance,
        ), None
    loader = OfflineDocumentLoader(
        object_value(
            parameters.get("resources", {}),
            location="worker request",
        ),
        max_bytes=limits.max_bytes,
    )
    if operation == "context_inspection":
        known_keys(
            parameters,
            {
                "context",
                "processing_mode",
                "base",
                "inherited_context",
                "scopes",
                "resources",
            },
            location="worker context inspection",
        )
        if "context" not in parameters:
            raise InputError("Context inspection requires an explicit context")
        inspection = inspect_context(
            parameters["context"],
            processing_mode=string_value(
                parameters.get("processing_mode", "json-ld-1.1")
            ),
            base=optional_string_value(parameters.get("base")),
            inherited_context=parameters.get("inherited_context", ABSENT),
            scopes=_context_scopes(parameters.get("scopes", []), limits),
            document_loader=loader,
            limits=limits,
        )
        return inspection.report, inspection.data
    if operation == "context_definitions":
        known_keys(
            parameters,
            {
                "before",
                "after",
                "terms",
                "scopes",
                "processing_mode",
                "before_base",
                "after_base",
                "before_inherited",
                "after_inherited",
                "compare_defaults",
                "resources",
                "before_resources",
                "after_resources",
            },
            location="worker context definitions",
        )
        if "before" not in parameters or "after" not in parameters:
            raise InputError("Context comparison requires both explicit contexts")
        raw_terms = parameters.get("terms")
        if raw_terms is not None and not isinstance(raw_terms, list):
            raise InputError("Selected context terms require an array or null")
        if isinstance(raw_terms, list) and len(raw_terms) > limits.max_states:
            raise LimitExceeded("Selected context terms exceed max_states")
        terms = (
            None
            if raw_terms is None
            else tuple(string_value(term) for term in raw_terms)
        )
        before_loader = (
            OfflineDocumentLoader(
                object_value(
                    parameters["before_resources"], location="worker before resources"
                ),
                max_bytes=limits.max_bytes,
            )
            if "before_resources" in parameters
            else loader
        )
        after_loader = (
            OfflineDocumentLoader(
                object_value(
                    parameters["after_resources"], location="worker after resources"
                ),
                max_bytes=limits.max_bytes,
            )
            if "after_resources" in parameters
            else loader
        )
        comparison = compare_context_definitions(
            parameters["before"],
            parameters["after"],
            terms=terms,
            scopes=_context_scopes(parameters.get("scopes", []), limits),
            processing_mode=string_value(
                parameters.get("processing_mode", "json-ld-1.1")
            ),
            before_base=optional_string_value(parameters.get("before_base")),
            after_base=optional_string_value(parameters.get("after_base")),
            before_inherited=parameters.get("before_inherited", ABSENT),
            after_inherited=parameters.get("after_inherited", ABSENT),
            compare_defaults=boolean_value(parameters.get("compare_defaults", True)),
            before_loader=before_loader,
            after_loader=after_loader,
            limits=limits,
        )
        return comparison.report, comparison.data
    if operation == "serialization":
        known_keys(
            parameters,
            {"before", "after", "before_base", "after_base", "resources"},
            location="worker request",
        )
        return compare_serializations(
            parameters.get("before"),
            parameters.get("after"),
            before_base=optional_string_value(parameters.get("before_base")),
            after_base=optional_string_value(parameters.get("after_base")),
            before_loader=loader,
            after_loader=loader,
            limits=limits,
        ), None
    common = {"document", "base", "resources"}
    base = optional_string_value(parameters.get("base"))
    document = parameters.get("document")
    if operation == "framing":
        known_keys(
            parameters,
            common
            | {
                "frame",
                "retained",
                "predicates",
                "omitted",
                "omitted_predicates",
                "direction",
            },
            location="worker framing request",
        )
        if "frame" not in parameters:
            raise InputError("Framing requires an explicit frame")
        omissions = array_value(parameters.get("omitted", []))
        if len(omissions) > limits.max_states:
            raise LimitExceeded("Framing record omissions exceed max_states")
        framed = frame_document(
            document,
            parameters["frame"],
            retained=_frame_selector(parameters.get("retained"), limits),
            predicates=_frame_strings(parameters.get("predicates"), limits),
            omitted=tuple(_frame_selector(item, limits) for item in omissions),
            omitted_predicates=_frame_strings(
                parameters.get("omitted_predicates", []), limits
            ),
            base=base,
            document_loader=loader,
            limits=limits,
            direction=cast(
                DirectionPolicy | None,
                optional_string_value(parameters.get("direction")),
            ),
        )
        return framed.report, {
            "document": framed.document,
            "base": framed.base,
            "nodes": [asdict(node) for node in framed.nodes],
            "omissions": [asdict(item) for item in framed.omissions],
        }
    if operation == "selection":
        known_keys(parameters, common | {"selector"}, location="worker request")
        prepared = prepare_usages(
            document, base=base, document_loader=loader, limits=limits
        )
        matches = prepared.select(selector_from_data(parameters.get("selector")))
        return matches.report, {
            "coverage_unit": matches.coverage_unit,
            "count": matches.count,
            "paths": [list(path) for path in matches.paths],
            "entities": [asdict(key) for key in matches.entities],
        }
    if operation == "context":
        known_keys(
            parameters,
            common | {"expectations"},
            location="worker request",
        )
        return check_context(
            document,
            expectations_from_data(parameters.get("expectations")),
            base=base,
            document_loader=loader,
            limits=limits,
        ), None
    if operation == "emission":
        allowance = Budget(limits)
        known_keys(
            parameters,
            common | {"models", "usages", "rdf_direction", "bindings", "vocabulary"},
            location="worker request",
        )
        models = models_from_data(parameters.get("models"), budget=allowance)
        if "bindings" in parameters:
            source_data = object_value(
                parameters.get("vocabulary"), location="vocabulary"
            )
            known_keys(
                source_data,
                {"source", "format", "base", "graph_name", "union"},
                location="vocabulary",
            )
            vocabulary = load_vocabulary(
                RdfSource(
                    string_value(source_data.get("source")),
                    format=optional_string_value(source_data.get("format")),
                    document_iri=optional_string_value(source_data.get("base")),
                    graph_name=optional_string_value(source_data.get("graph_name")),
                    union=boolean_value(source_data.get("union", False)),
                ),
                budget=allowance,
            )
            result = check_serialized(
                document,
                models,
                usages_from_data(parameters.get("usages"), budget=allowance),
                vocabulary=vocabulary,
                bindings=bindings_from_data(parameters["bindings"], allowance),
                rdf_direction=cast(
                    DirectionPolicy | None,
                    optional_string_value(parameters.get("rdf_direction")),
                ),
                base=base,
                document_loader=loader,
                budget=allowance,
            )
            observation = result.observed
            try:
                bindings_data: list[dict[str, object]] = []
                if observation is not None:
                    allowance.consume("states", len(observation.bindings))
                    allowance.consume("bytes", 128 + 512 * len(observation.bindings))
                    for item in observation.bindings:
                        bound_text(item.identity.owner, allowance)
                        allowance.consume("states", len(item.identity.path))
                        allowance.consume("bytes", 64 + 16 * len(item.identity.path))
                        allowance.consume("work", len(item.identity.path) + 1)
                        if item.exclusion is not None:
                            bound_text(item.exclusion, allowance)
                        bindings_data.append(
                            {
                                "owner": item.identity.owner,
                                "path": list(item.identity.path),
                                "report": report_to_data(item.report, allowance),
                                "exclusion": item.exclusion,
                            }
                        )
                return result.report, {
                    "declarations": declaration_data(
                        observation.declarations, allowance
                    )
                    if observation
                    else [],
                    "bindings": bindings_data,
                }
            except LimitExceeded:
                return combine_reports(
                    "serialization.sample",
                    result.report,
                    Report(
                        "serialization.bindings",
                        requested=1,
                        complete=False,
                        issues=(
                            Issue(
                                "execution.limit_exceeded",
                                "Serialized binding publication exceeded its allowance",
                                kind="exhausted",
                            ),
                        ),
                    ),
                ), None
        if "vocabulary" in parameters:
            raise InputError("A vocabulary requires explicit serialized bindings")
        return check_emission(
            document,
            models,
            usages_from_data(parameters.get("usages")),
            rdf_direction=cast(
                DirectionPolicy | None,
                optional_string_value(parameters.get("rdf_direction")),
            ),
            base=base,
            document_loader=loader,
            budget=allowance,
        ), None
    known_keys(
        parameters,
        common | {"correspondence"},
        location="worker request",
    )
    flat = flatten_document(
        document,
        base=base,
        document_loader=loader,
        limits=limits,
        correspondence=boolean_value(parameters.get("correspondence", False)),
    )
    return flat.report, {
        "document": flat.document,
        "nodes": [
            {
                "identifier": node.identifier,
                "graph": node.graph,
                "path": list(node.path),
                "value": node.value,
                "expanded_paths": [list(path) for path in node.expanded_paths],
            }
            for node in flat.nodes
        ],
        "correspondence_report": flat.correspondence_report.to_dict()
        if flat.correspondence_report
        else None,
    }
