"""Bound selected native/Python values before finite local terminology checks."""

import json
from collections.abc import Iterator
from dataclasses import is_dataclass
from importlib import import_module
from typing import Any, cast

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import SourcePath, bound_path
from bijux_ontology.json.values import measure_json
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.languages.matching import matches_language

from ..admission import (
    NativeFidelityUnavailable,
    NativeVersionUnavailable,
    admit_r5_value,
    r5_models,
)
from ..code_system.reading import read_code_system
from ..code_system.types import FhirCodeSystem
from ..identity import FhirCodeBinding
from ..json import decode_fhir_json
from ..value_set.composition import compile_value_set
from ..value_set.types import FhirValueSet, FhirValueSetPolicy, FhirValueSetSelection
from .types import FhirCodingBinding, FhirDisplayPolicy


def reserve(budget: Budget, count: int = 1) -> None:
    """Prepay retained source/result references before allocation or child work."""
    budget.consume("work", count + 1)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)


def diagnostic(
    issues: IssueCollector,
    code: str,
    message: str,
    path: SourcePath = (),
    *,
    unknown: bool = False,
    warning: bool = False,
) -> None:
    """Retain concrete field coordinates and semantic severity independently."""
    bound_path(path, issues.budget)
    issues.append(
        Issue(
            "adapter.fhir_coding_" + code,
            message,
            kind="unverified" if unknown else "finding",
            severity="warning" if warning else "error",
            details=Location(path=path),
        )
    )


def adopt(
    report: Report, budget: Budget, issues: IssueCollector, reports: list[Report]
) -> None:
    """Keep completed paid diagnostics before fallible raw-report adoption."""
    reports.append(report)
    issues.retain_completed(report)
    retain_report(report, budget, issues)


def encode(value: object, budget: Budget) -> str:
    """Serialize already captured JSON after measuring escaped workspace."""
    size = measure_json(
        value,
        budget=budget,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
    ).weight
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Selected FHIR JSON exceeds max_bytes")
    budget.consume("bytes", 16 * (size + 1))
    budget.consume("work", size + 1)
    result = json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )
    if len(result) > budget.limits.max_bytes:
        raise LimitExceeded("Selected FHIR JSON exceeds max_bytes")
    return result


def native_types() -> tuple[Any, Any, Any]:
    """Load actual optional R5 models lazily, without a mandatory core dependency."""
    try:
        coding, concept = r5_models("Coding", "CodeableConcept")
        return coding, concept, import_module("pydantic").BaseModel
    except NativeVersionUnavailable as error:
        raise ResourceError(
            "Coding bindings require the maintained FHIR R5 model"
        ) from error
    except ModuleNotFoundError as error:
        raise ResourceError(
            "Install bijux-ontology[fhir] for maintained R5 coding models"
        ) from error


def _data(
    value: object, budget: Budget, native: tuple[Any, Any, Any], depth: int = 0
) -> object:
    """Capture supported selected fields once, before native model serialization."""
    budget.consume("work")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Selected FHIR source exceeds max_depth")
    coding, concept, _ = native
    if type(value) is coding or type(value) is concept:
        allowed = (
            {"id", "system", "version", "code", "display", "userSelected"}
            if type(value) is coding
            else {"id", "coding", "text"}
        )
        fields = cast(Any, coding if type(value) is coding else concept).model_fields
        reserve(budget, len(fields))
        result = {}
        for name in fields:
            budget.consume("work")
            raw = value.__dict__.get(name)
            if raw is None:
                continue
            if name not in allowed:
                raise ResourceError(
                    "FHIR primitive extensions/comments need unsupported interpretation"
                )
            result[name] = _data(raw, budget, native, depth + 1)
        return result
    if type(value) is list:
        selected = capture(value, budget)
        reserve(budget, len(selected))
        return [_data(item, budget, native, depth + 1) for item in selected]
    if type(value) is dict:
        reserve(budget, len(value))
        result = {}
        for name, raw in value.items():
            if type(name) is not str:
                raise InputError("FHIR source object keys require text")
            bound_text(name, budget)
            if name.startswith("_") or name in {
                "extension",
                "modifierExtension",
                "implicitRules",
            }:
                raise ResourceError(
                    "FHIR extensions/modifiers need unsupported interpretation"
                )
            result[name] = _data(raw, budget, native, depth + 1)
        return result
    if type(value) is str:
        bound_text(value, budget)
        return value
    if value is None or type(value) in {bool, int}:
        return value
    raise InputError(
        "Selected FHIR fields require original exact supported JSON scalars"
    )


def native_value(
    value: object, datatype: str, budget: Budget, native: tuple[Any, Any, Any]
) -> tuple[object, str]:
    """Use actual strict R5 types and reject original scalar coercion/loss."""
    raw = _data(value, budget, native)
    source = encode(raw, budget)
    budget.consume("bytes", 32 * len(source) + 256)
    budget.consume("work", 8 * len(source) + 1)
    if datatype == "code":
        if type(raw) is not str:
            raise InputError("A bare FHIR code requires an actual string")
        expected = {"code": raw}
        model = native[0]
    else:
        if type(raw) is not dict:
            raise InputError("Coding/CodeableConcept require an actual object")
        if (
            datatype == "CodeableConcept"
            and not raw.get("coding")
            and not raw.get("text")
        ):
            raise InputError(
                "CodeableConcept requires coding or text beyond Element.id"
            )
        model = native[0] if datatype == "Coding" else native[1]
        expected = raw
    try:
        admit_r5_value(model, expected, budget)
    except NativeFidelityUnavailable as error:
        raise ResourceError(
            "Native FHIR production changes selected source values"
        ) from error
    budget.consume("work", 0)
    return raw, source


def occurrences(
    record: object, path: tuple[str, ...], budget: Budget, native: tuple[Any, Any, Any]
) -> Iterator[tuple[SourcePath, object]]:
    """Visit every selected repeated parent, retaining concrete field coordinates."""

    def walk(
        value: object, remaining: tuple[str, ...], where: SourcePath, depth: int
    ) -> Iterator[tuple[SourcePath, object]]:
        budget.consume("work")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Selected field traversal exceeds max_depth")
        if remaining and type(value) in {list, tuple}:
            items = capture(cast(list[object] | tuple[object, ...], value), budget)
            if not items:
                yield (*where, *remaining), ABSENT
            for index, child in enumerate(items):
                yield from walk(child, remaining, (*where, index), depth + 1)
        elif not remaining:
            yield where, value
        else:
            name, rest = remaining[0], remaining[1:]
            selected: object = ABSENT
            if type(value) is dict:
                selected = value.get(name, ABSENT)
            elif isinstance(value, native[2]):
                if name in type(value).model_fields:
                    selected = value.__dict__.get(name, ABSENT)
                    if selected is None:
                        selected = ABSENT
            elif is_dataclass(value) and not isinstance(value, type):
                if name in type(value).__dataclass_fields__:
                    selected = getattr(value, name)
            elif value is not ABSENT:
                raise InputError(
                    "Intermediate fields need plain objects or native models"
                )
            if selected is ABSENT:
                yield (*where, *remaining), ABSENT
            else:
                yield from walk(selected, rest, (*where, name), depth + 1)

    yield from walk(record, path, (), 0)


def binding_contract(binding: FhirCodingBinding, budget: Budget) -> None:
    """Qualify every caller field before keyed lookup, native work or reports."""
    if type(binding) is not FhirCodingBinding or type(binding.path) is not tuple:
        raise InputError("Use an ordinary FhirCodingBinding with a concrete field path")
    reserve(budget, len(binding.path))
    if any(type(part) is not str or not part for part in binding.path):
        raise InputError("FHIR binding paths contain exact nonempty field names")
    bound_path(binding.path, budget)
    for text in (
        binding.datatype,
        binding.strength,
        binding.mode,
        binding.value_set_reference,
    ):
        bound_text(text, budget)
    if (
        binding.datatype not in {"code", "Coding", "CodeableConcept"}
        or binding.strength not in {"required", "extensible", "preferred", "example"}
        or binding.mode not in {"compose", "expansion"}
        or type(binding.value_set) is not FhirValueSet
    ):
        raise InputError(
            "Unknown FHIR datatype, strength or original ValueSet contract"
        )
    if type(binding.external_value_set) is not bool:
        raise InputError("External ValueSet status requires an explicit boolean")
    if type(binding.minimum_occurrences) is not int:
        raise InputError("Minimum occurrences must be an explicit nonnegative integer")
    for number in (binding.minimum_occurrences, binding.maximum_occurrences):
        if number is not None and (
            type(number) is not int or number < 0 or number.bit_length() > 63
        ):
            raise InputError(
                "FHIR occurrence counts require finite nonnegative integers"
            )
    if (
        binding.maximum_occurrences is not None
        and binding.minimum_occurrences > binding.maximum_occurrences
    ):
        raise InputError("FHIR occurrence minimum exceeds maximum")
    display = binding.display
    if (
        type(display) is not FhirDisplayPolicy
        or display.mode not in {"ignore", "defined", "language"}
        or type(display.require_display) is not bool
        or type(display.ranges) is not tuple
        or type(display.fallback) is not tuple
    ):
        raise InputError("Display checks need an explicit finite policy")
    for text in (*display.ranges, *display.fallback):
        bound_text(text, budget)
    if any(tier not in {"untagged", "other"} for tier in display.fallback) or len(
        set(display.fallback)
    ) != len(display.fallback):
        raise InputError("Display fallback tiers must be distinct supported choices")
    for language_range in display.ranges:
        matches_language(None, language_range, budget=budget)
    if display.mode == "ignore" and display.require_display:
        raise InputError("Requiring display needs an active display policy")
    if display.mode == "language" and not display.ranges:
        raise InputError("Language display policy requires explicit RFC4647 ranges")
    if display.mode != "language" and (display.ranges or display.fallback):
        raise InputError("Language preferences require language display mode")


def local_systems(
    systems: tuple[FhirCodeSystem, ...], budget: Budget, reports: list[Report]
) -> tuple[FhirCodeSystem, ...]:
    """Reacquire original native resources with their exact caller RDF identities."""
    if type(systems) is not tuple:
        raise InputError("Local CodeSystems require an immutable finite tuple")
    reserve(budget, len(systems))
    result = []
    for system in systems:
        if type(system) is not FhirCodeSystem:
            raise InputError("Local terminology requires owned CodeSystems")
        reserve(budget)
        acquired = read_code_system(
            system.payload,
            bindings=system.bindings,
            case_comparison=system.case_comparison,
            budget=budget,
        )
        reports.append(acquired.report)
        result.append(acquired)
    return tuple(result)


def selection(
    binding: FhirCodingBinding,
    systems: tuple[FhirCodeSystem, ...],
    policy: FhirValueSetPolicy,
    budget: Budget,
) -> tuple[FhirValueSetSelection, bool]:
    """Use the actual178 compiler; never accept a caller asserted complete set."""
    parts = binding.value_set_reference.split("|")
    if len(parts) > 2 or any(not part for part in parts):
        raise InputError("ValueSet reference needs an unambiguous R5 canonical")
    absolute_iri(parts[0])
    raw = decode_fhir_json(binding.value_set.payload, budget)
    if not isinstance(raw, dict):
        raise InputError("Original ValueSet must remain an object")
    matches = raw.get("url") == parts[0] and raw.get("version") == (
        parts[1] if len(parts) == 2 else None
    )
    bindings: list[FhirCodeBinding] = []
    for system in systems:
        reserve(budget, len(system.bindings))
        bindings.extend(system.bindings)
    result = compile_value_set(
        binding.value_set,
        mode=binding.mode,
        systems=systems,
        bindings=tuple(bindings),
        policy=policy,
        budget=budget,
    )
    return result, matches
