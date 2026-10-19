"""Finite public operation dispatch; no executable callbacks cross the protocol."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import cast

from bijux_ontology._json import (
    boolean_value,
    known_keys,
    object_value,
    optional_string_value,
    string_value,
)
from bijux_ontology.declarations.codecs import models_from_data, terms_from_data
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.checking import check_context, prepare_usages
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.comparison import compare_serializations
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.flattening import flatten_document
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.jsonld.selectors.codecs import selector_from_data
from bijux_ontology.limits import Limits
from bijux_ontology.models.codecs import usages_from_data
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig

OPERATIONS = frozenset(
    (
        "context",
        "emission",
        "serialization",
        "flatten",
        "vocabulary",
        "project",
        "selection",
    )
)


def execute_operation(
    operation: str,
    parameters: dict[str, object],
    limits: Limits,
    completed: Callable[[Report], None],
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
        return check_project(project, completed=completed), None
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
        vocabulary = load_vocabulary(source, limits=limits)
        return check_vocabulary(
            vocabulary, terms_from_data(parameters.get("terms")), limits=limits
        ), None
    loader = OfflineDocumentLoader(
        object_value(
            parameters.get("resources", {}),
            location="worker request",
        ),
        max_bytes=limits.max_bytes,
    )
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
        known_keys(
            parameters,
            common | {"models", "usages", "rdf_direction"},
            location="worker request",
        )
        return check_emission(
            document,
            models_from_data(parameters.get("models")),
            usages_from_data(parameters.get("usages")),
            rdf_direction=cast(
                DirectionPolicy | None,
                optional_string_value(parameters.get("rdf_direction")),
            ),
            base=base,
            document_loader=loader,
            limits=limits,
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
