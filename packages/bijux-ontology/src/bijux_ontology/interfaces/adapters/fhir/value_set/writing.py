"""Actual R5 native production with exact retained source-inventory comparison."""

from importlib import import_module

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)

from ..code_system.reading import _operation as operation
from ..json import UnsupportedJsonNumber, decode_fhir_json, same_json
from .types import FhirValueSet, FhirValueSetExport


def write_value_set(
    value_set: FhirValueSet,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FhirValueSetExport:
    """Produce original supported trees/parameters through the maintained model.

    Export does not upgrade a page, resolve a compose clause or assert that a
    supplied expansion and definition are equivalent. Source fidelity and finite
    selection completeness remain separate required contracts.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    children: tuple[Report, ...] = ()
    content = None
    checked = 0
    try:
        if not isinstance(value_set, FhirValueSet):
            raise InputError("Expected an owned R5 ValueSet")
        retain_report(value_set.report, owned_budget, issues)
        children = (value_set.report,)
        if not value_set.report.verified:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_unverified",
                    "The supplied ValueSet source is "
                    "not qualified for native production",
                    kind="unverified",
                )
            )
        else:
            if len(value_set.payload) > owned_budget.limits.max_bytes:
                raise LimitExceeded("ValueSet export exceeds max_bytes")
            owned_budget.consume("bytes", 6 * len(value_set.payload))
            owned_budget.consume("work", 5 * len(value_set.payload) + 1)
            raw = decode_fhir_json(value_set.payload, owned_budget)
            native_type = import_module("fhir.resources.valueset").ValueSet
            native = native_type.model_validate_json(value_set.payload, strict=True)
            produced = native.model_dump_json()
            if len(produced) > owned_budget.limits.max_bytes:
                raise LimitExceeded("Native ValueSet export exceeds max_bytes")
            native_type.model_validate_json(produced, strict=True)
            if not same_json(
                decode_fhir_json(produced, owned_budget), raw, owned_budget
            ):
                issues.append(
                    Issue(
                        "adapter.fhir_valueset_unverified",
                        "Native production changed the "
                        "retained source tree or parameters",
                        kind="unverified",
                    )
                )
            else:
                owned_budget.consume("work", 0)
                content = produced.encode("utf-8")
                checked = 1
    except ModuleNotFoundError:
        try:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_unverified",
                    "Install bijux-ontology[fhir] for the maintained R5 producer",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining optional producer qualification")
    except UnsupportedJsonNumber as error:
        try:
            issues.append(
                Issue("adapter.fhir_valueset_unverified", str(error), kind="unverified")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining unsupported native numeric context")
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, UnicodeError):
        try:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_input_error",
                    "The actual R5 producer rejected the supplied source",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining native ValueSet failure")
    report = Report(
        "adapter.fhir_value_set_export",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1,
        children=children,
        regime="fhir-r5-native-value-set-producer",
    )
    return FhirValueSetExport(content if report.verified else None, report)
