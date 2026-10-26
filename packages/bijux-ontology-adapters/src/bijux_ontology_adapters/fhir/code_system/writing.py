"""Reconstruct supported concept records through the actual maintained producer."""

import json
from importlib import import_module
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)

from ..json import same_json
from .reading import _KINDS, _operation
from .types import FhirCodeSystem, FhirCodeSystemExport, FhirConcept


def _concept(concept: FhirConcept) -> dict[str, object]:
    """Rebuild supported native fields from owned typed records."""
    record: dict[str, object] = {"code": concept.identity.code}
    if concept.display is not None:
        record["display"] = concept.display
    if concept.definition is not None:
        record["definition"] = concept.definition
    if concept.designations:
        designations = []
        for item in concept.designations:
            designation: dict[str, object] = {"value": item.value}
            if item.language is not None:
                designation["language"] = item.language
            if item.use_json is not None:
                designation["use"] = json.loads(item.use_json)
            if item.additional_uses_json:
                designation["additionalUse"] = [
                    json.loads(value) for value in item.additional_uses_json
                ]
            designations.append(designation)
        record["designation"] = designations
    if concept.properties:
        record["property"] = [
            {"code": item.code, _KINDS[item.kind]: item.value}
            for item in concept.properties
        ]
    return record


def write_code_system(
    system: FhirCodeSystem,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FhirCodeSystemExport:
    """Produce and reparse real R5 JSON; never replay an unqualified input blob."""
    if not isinstance(system, FhirCodeSystem):
        raise InputError("Expected an owned FHIR CodeSystem")
    operation = _operation(limits, budget)
    issues = IssueCollector(operation)
    output = None
    checked = 0
    source_reports: tuple[Report, ...] = ()
    try:
        retain_report(system.report, operation, issues)
        source_reports = (system.report,)
        if not system.report.verified:
            issues.append(
                Issue(
                    "adapter.fhir_export_unverified",
                    "CodeSystem interpretation is not qualified for export",
                    kind="unverified",
                )
            )
        else:
            if len(system.payload) > operation.limits.max_bytes:
                raise LimitExceeded("CodeSystem export exceeds max_bytes")
            operation.consume("bytes", 5 * len(system.payload))
            operation.consume("work", 5 * len(system.payload) + 1)
            operation.consume("states", len(system.concepts))
            raw = cast(dict[str, object], json.loads(system.document_json))
            roots: list[dict[str, object]] = []
            records = {}
            for concept in system.concepts:
                operation.consume("work")
                record = _concept(concept)
                records[concept.identity] = record
                if concept.parent is None:
                    roots.append(record)
                else:
                    children = records[concept.parent].setdefault("concept", [])
                    cast(list[dict[str, object]], children).append(record)
            if "concept" in raw or roots:
                raw["concept"] = roots
            native_type = import_module("fhir.resources.codesystem").CodeSystem
            native = native_type.model_validate(raw, strict=True)
            produced = native.model_dump_json()
            if len(produced) > operation.limits.max_bytes:
                raise LimitExceeded("Native CodeSystem export exceeds max_bytes")
            native_type.model_validate_json(produced, strict=True)
            # Exact JSON equality covers typed scalars and all retained metadata;
            # hierarchy and designation Coding qualifiers cannot disappear here.
            if not same_json(
                json.loads(produced), json.loads(system.document_json), operation
            ):
                issues.append(
                    Issue(
                        "adapter.fhir_export_unverified",
                        "Maintained producer changed the supported source inventory",
                        kind="unverified",
                    )
                )
            else:
                output = produced.encode("utf-8")
                checked = 1
    except ModuleNotFoundError:
        try:
            issues.append(
                Issue(
                    "adapter.fhir_backend_unavailable",
                    "Install bijux-ontology-adapters[fhir] for the maintained R5 model",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining missing native producer evidence")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (ValueError, TypeError, KeyError, RecursionError):
        try:
            issues.append(
                Issue(
                    "adapter.fhir_export_error",
                    "Maintained R5 producer rejected the supported source",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining native producer failure")
    report = Report(
        "adapter.fhir_code_system_export",
        checked,
        tuple(issues),
        requested=1,
        complete=not any(item.kind in {"unverified", "exhausted"} for item in issues),
        children=source_reports,
        regime="fhir-r5-native-producer",
    )
    return FhirCodeSystemExport(output if report.verified else None, report)
