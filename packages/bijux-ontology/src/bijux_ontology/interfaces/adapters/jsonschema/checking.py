"""Check exact real serializer samples against explicit schema/RDF contracts."""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from rdflib import Graph

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.adapters.jsonschema.extraction import (
    _backend,
    _charge,
    _operation,
    _owned,
    _Schema,
)
from bijux_ontology.interfaces.adapters.jsonschema.types import SchemaModel
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.paths import SourcePath, location
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.emission.serialization import InstanceCheck, check_serialized
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.vocabulary.sources.view import Vocabulary


def _pointer(path: SourcePath, budget: Budget) -> str:
    """Bound the complete escaped diagnostic pointer before constructing it."""
    _charge(path, budget)
    size = 0
    for part in path:
        text = str(part)
        size += 1 + len(text.encode("utf-8")) + text.count("~") + text.count("/")
        if size > budget.limits.max_bytes:
            raise LimitExceeded("Schema diagnostic pointer exceeds max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", size)
    return location(path)


def _bindings(
    prepared: _Schema,
    node: Any,
    resolver: Any,
    value: object,
    path: SourcePath,
    selected: list[ModelUsage],
    depth: int = 0,
) -> None:
    """Bind only actual objects in explicit finite schema alternatives."""
    budget = prepared.budget
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Sample schema traversal exceeds max_depth")
    if not isinstance(node, dict):
        return
    actual, resolver = prepared.resolve(node, resolver)
    if actual is not node:
        _bindings(prepared, actual, resolver, value, path, selected, depth + 1)
        return
    if "oneOf" in node:
        branches = node["oneOf"]
        discriminator = prepared.discriminator(branches, resolver)
        if discriminator is None or not isinstance(value, dict):
            prepared.unknown(
                "Actual output has no established discriminated object branch"
            )
            return
        tag = value.get(discriminator, ABSENT)
        matching = []
        for branch in branches:
            budget.consume("work")
            resolved, _ = prepared.resolve(branch, resolver)
            wanted = resolved["properties"][discriminator]["const"]
            if type(tag) is type(wanted) and tag == wanted:
                matching.append(branch)
        if len(matching) != 1:
            prepared.unknown(
                "Actual discriminator does not select one declared schema branch"
            )
            return
        _bindings(prepared, matching[0], resolver, value, path, selected, depth + 1)
        return
    if isinstance(value, dict):
        mapping = prepared.models.get(id(node))
        if mapping is not None:
            selected.append(ModelUsage(mapping.identifier, path))
            for key in value:
                budget.consume("work")
                if key not in node.get("properties", {}) and key not in {
                    "@context",
                    "@id",
                    "@type",
                }:
                    prepared.unknown(
                        "Actual additional property has no explicit RDF field mapping"
                    )
        for key, child in node.get("properties", {}).items():
            budget.consume("work")
            if key in value and key not in {"@context", "@id", "@type"}:
                _bindings(
                    prepared,
                    child,
                    resolver,
                    value[key],
                    (*path, key),
                    selected,
                    depth + 1,
                )
    elif isinstance(value, list) and "items" in node:
        for index in range(len(value)):
            _bindings(
                prepared,
                node["items"],
                resolver,
                value[index],
                (*path, index),
                selected,
                depth + 1,
            )


def check_instance(
    instance: object,
    serializer: Callable[..., object],
    schema: object,
    mappings: Sequence[SchemaModel],
    usages: Sequence[ModelUsage] | None = None,
    *,
    vocabulary: Vocabulary | Graph,
    options: Mapping[str, object],
    resources: Mapping[str, object] | None = None,
    output_schema: bool = False,
    context: object | Absent = ABSENT,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
) -> InstanceCheck:
    """Call the serializer once and retain actual JSON/output and RDF qualifications.

    ``output_schema=True`` explicitly applies JSON Schema assertions to this
    serialized sample. Input schemas otherwise supply structure and mappings
    only. Neither mode converts ``required`` into an RDF emission guarantee.
    Optional usages must agree with established schema object/branch paths.
    """
    if type(output_schema) is not bool:
        raise InputError("output_schema must be a boolean")
    operation = _operation(limits, budget)
    _backend()
    issues = IssueCollector(operation)
    children: list[Report] = []
    output_json = None
    try:
        prepared = _Schema(schema, mappings, resources or {}, operation, issues)
        extraction = prepared.extraction()
        children.append(extraction.report)
        issues = IssueCollector(operation)
        _charge(options, operation)
        supplied = _owned(dict(options), operation)
        if not isinstance(supplied, dict):
            raise InputError("Serializer options require a named mapping")
        output = _owned(serializer(instance, **supplied), operation)
        encoded = json.dumps(
            output, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        operation.consume("bytes", len(encoded.encode("utf-8")))
        output_json = encoded
        # Precharge a conservative schema/input product before maintained work.
        # Acyclic reference cost includes repeated applications, not only nodes.
        input_cost = _charge(output, operation)
        operation.consume("work", prepared.validation_cost * input_cost)
        checked = 0
        if output_schema and prepared.safe:
            for error in prepared.validator.iter_errors(output):
                operation.consume("work")
                operation.consume("states")
                path = tuple(error.absolute_path)
                where = _pointer(path, operation)
                issues.append(
                    Issue(
                        "adapter.schema_mismatch",
                        "Actual serialized output violates its explicit JSON Schema",
                        where,
                    )
                )
            checked = 1
        elif output_schema:
            prepared.unknown(
                "Unsupported schema scope cannot qualify actual output validation"
            )
        selected: list[ModelUsage] = []
        if prepared.safe:
            _bindings(
                prepared, prepared.schema, prepared.resolver, output, (), selected
            )
        else:
            prepared.unknown("Unsupported schema scope cannot establish model bindings")
        if not selected:
            prepared.unknown("No actual mapped output object was established")
        if usages is not None:
            if not isinstance(usages, Sequence) or isinstance(
                cast(object, usages), (str, bytes)
            ):
                raise InputError("Expected a finite model usage sequence")
            count = len(usages)
            if count > operation.limits.max_states:
                raise LimitExceeded("Model usages exceed max_states")
            for index in range(count):
                requested = usages[index]
                _charge(requested, operation)
                if requested not in selected:
                    raise InputError(
                        "Model usage does not match the actual schema branch and path"
                    )
        qualification = Report(
            "jsonschema.output",
            checked=checked,
            requested=1 if output_schema else 0,
            allow_empty=not output_schema,
            issues=tuple(issues),
            children=(*children, prepared.report()),
            families=("sampled-output",),
        )
        return check_serialized(
            output,
            extraction.declarations,
            selected,
            vocabulary=vocabulary,
            context=context,
            declaration_report=qualification,
            base=base,
            document_loader=document_loader,
            budget=operation,
            rdf_direction=rdf_direction,
        )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("JSON Schema sample checking exceeded its limits")
    except Exception:
        try:
            issues.append(
                Issue(
                    "input.invalid",
                    "Schema or serializer input could not be processed",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("JSON Schema diagnostics exceeded their limit")
    failure = Report(
        "jsonschema.output",
        requested=1,
        complete=False,
        issues=tuple(issues),
        children=tuple(children),
    )
    return InstanceCheck(
        output_json, None, combine_reports("serialization.sample", failure)
    )
