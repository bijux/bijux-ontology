"""Local original JSON and actual maintained R5 ValueSet interpretation."""

import json
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.values import array_value, object_value, text_value
from bijux_ontology.rdf.identifiers import absolute_iri

from ..admission import (
    NativeFidelityUnavailable,
    NativeVersionUnavailable,
    admit_r5_json,
)
from ..code_system.reading import _operation as operation
from ..identity import FhirCodeIdentity
from ..json import UnsupportedJsonNumber, decode_fhir_json
from .types import FhirExpansionEntry, FhirSelectionClause, FhirValueSet


class _Unsupported(Exception):
    """Preserved source requires semantics outside this finite R5 subset."""


def _object(value: object) -> dict[str, object]:
    """Require an original JSON object without maintained-model coercion."""
    return object_value(value, location="")


def _text(value: object) -> str:
    """Require actual nonempty JSON text for interpreted identity fields."""
    return text_value(value, location="")


def _optional(record: dict[str, object], key: str) -> str | None:
    """Keep absent identity text distinct from an invalid explicit null."""
    return _text(record[key]) if key in record else None


def _encode(value: object) -> str:
    """Serialize a previously bounded exact supported JSON inventory."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _source_kinds(raw: object, budget: Budget) -> None:
    """Reject native coercion and unqualified modifier/decimal interpretations."""
    pending = [raw]
    while pending:
        budget.consume("work")
        value = pending.pop()
        if isinstance(value, dict):
            for key, item in value.items():
                if key.startswith("_") or key in {
                    "extension",
                    "modifierExtension",
                    "implicitRules",
                    "contained",
                }:
                    raise _Unsupported(
                        "FHIR extensions and modifier context are not interpreted"
                    )
                if (
                    key
                    in {
                        "abstract",
                        "inactive",
                        "experimental",
                        "immutable",
                        "valueBoolean",
                    }
                    and type(item) is not bool
                ):
                    raise InputError("FHIR flags require original JSON booleans")
                if key in {"total", "offset", "valueInteger"} and type(item) is not int:
                    raise InputError(
                        "FHIR integer fields require original JSON integers"
                    )
                if key == "valueDecimal":
                    raise _Unsupported(
                        "Exact decimal context is outside the native fidelity subset"
                    )
                pending.append(item)
        elif isinstance(value, list):
            pending.extend(value)


def _clauses(
    raw: dict[str, object], key: str, budget: Budget
) -> tuple[FhirSelectionClause, ...]:
    """Capture ordered clauses while enforcing R5's actual source invariants."""
    clauses = []
    for item in array_value(raw.get(key, [])):
        budget.consume("work")
        budget.consume("states")
        record = _object(item)
        system, version = _optional(record, "system"), _optional(record, "version")
        if system is not None:
            absolute_iri(system)
        imported = tuple(
            _text(value) for value in array_value(record.get("valueSet", []))
        )
        filtered = bool(array_value(record.get("filter", [])))
        concepts = array_value(record.get("concept", []))
        if system is None and (not imported or concepts or filtered):
            raise InputError(
                "R5 include/exclude requires its own system or imported ValueSet"
            )
        if concepts and filtered:
            raise InputError("R5 include/exclude cannot combine concepts and filters")
        codes = (
            tuple(_text(_object(value).get("code")) for value in concepts)
            if "concept" in record
            else None
        )
        clauses.append(
            FhirSelectionClause(
                system, version, codes, filtered, imported, _encode(record)
            )
        )
    return tuple(clauses)


def _entries(
    raw: dict[str, object], budget: Budget, path: tuple[int, ...] = ()
) -> tuple[FhirExpansionEntry, ...]:
    """Retain each presentation occurrence without inheriting parent identity."""
    entries = []
    for index, item in enumerate(array_value(raw.get("contains", []))):
        budget.consume("work")
        budget.consume("states")
        record = _object(item)
        code, system = _optional(record, "code"), _optional(record, "system")
        version, display = _optional(record, "version"), _optional(record, "display")
        abstract = cast(bool, record.get("abstract", False))
        if code is None and (display is None or not abstract):
            raise InputError("R5 noncoded entries require a display and abstract=true")
        if code is not None and system is None:
            raise InputError("Every coded R5 entry requires its own system")
        if system is not None:
            absolute_iri(system)
        identity = (
            FhirCodeIdentity(system, version, code)
            if system is not None and code is not None
            else None
        )
        own_path = (*path, index)
        metadata = {key: value for key, value in record.items() if key != "contains"}
        entries.append(
            FhirExpansionEntry(
                own_path,
                identity,
                display,
                abstract,
                cast(bool | None, record.get("inactive")),
                _encode(metadata),
                _entries(record, budget, own_path),
            )
        )
    return tuple(entries)


def read_value_set(
    content: bytes | str, *, limits: Limits | None = None, budget: Budget | None = None
) -> FhirValueSet:
    """Capture supplied R5 JSON without fetching pages, imports or terminology.

    Native acceptance is supplemented by original scalar kinds and R5's own
    include/contains invariants. Filters and parameters remain source evidence;
    reading does not declare their selected set complete or execute them.
    """
    owned_budget = operation(limits, budget)
    issues = IssueCollector(owned_budget)
    payload, document_json = b"", "{}"
    includes: tuple[FhirSelectionClause, ...] = ()
    excludes: tuple[FhirSelectionClause, ...] = ()
    entries: tuple[FhirExpansionEntry, ...] = ()
    checked = 0
    try:
        if type(content) not in (bytes, str):
            raise InputError("ValueSet requires actual JSON bytes or text")
        if len(content) > owned_budget.limits.max_bytes:
            raise LimitExceeded("ValueSet source exceeds max_bytes")
        if isinstance(content, str):
            owned_budget.consume("work", len(content) + 1)
            owned_budget.consume("bytes", 4 * len(content))
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        if len(encoded) > owned_budget.limits.max_bytes:
            raise LimitExceeded("ValueSet source exceeds max_bytes")
        owned_budget.consume("resources")
        owned_budget.consume("bytes", 6 * len(encoded))
        owned_budget.consume("work", 5 * len(encoded) + 1)
        payload = encoded
        raw = _object(decode_fhir_json(payload, owned_budget))
        document_json = _encode(raw)
        if raw.get("resourceType") != "ValueSet" or raw.get("status") not in {
            "draft",
            "active",
            "retired",
            "unknown",
        }:
            raise InputError("Expected an actual R5 ValueSet with publication status")
        _source_kinds(raw, owned_budget)
        if "url" in raw:
            absolute_iri(_text(raw["url"]))
        if "compose" in raw:
            compose = _object(raw["compose"])
            includes, excludes = (
                _clauses(compose, "include", owned_budget),
                _clauses(compose, "exclude", owned_budget),
            )
            if not includes:
                raise InputError("R5 composition requires at least one include")
        if "expansion" in raw:
            expansion = _object(raw["expansion"])
            for key in ("offset", "total"):
                if key in expansion and cast(int, expansion[key]) < 0:
                    raise InputError("Expansion counts cannot be negative")
            entries = _entries(expansion, owned_budget)
        try:
            admit_r5_json(payload, raw, "ValueSet", owned_budget)
        except NativeVersionUnavailable as error:
            raise _Unsupported(
                "The installed maintained model is not FHIR R5"
            ) from error
        except NativeFidelityUnavailable as error:
            raise _Unsupported(
                "Native R5 production changes original source values"
            ) from error
        owned_budget.consume("work", 0)
        checked = 1
    except ModuleNotFoundError:
        try:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_unverified",
                    "Install bijux-ontology-adapters[fhir] for the maintained R5 model",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the missing R5 model qualification")
    except (_Unsupported, UnsupportedJsonNumber) as error:
        try:
            issues.append(
                Issue("adapter.fhir_valueset_unverified", str(error), kind="unverified")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining ValueSet interpretation qualification")
    except (LimitExceeded, MemoryError, RecursionError) as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, UnicodeError):
        try:
            issues.append(
                Issue(
                    "adapter.fhir_valueset_input_error",
                    "Original ValueSet source violates the supported R5 contract",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the original R5 input failure")
    report = Report(
        "adapter.fhir_value_set",
        checked,
        tuple(issues),
        requested=1,
        complete=checked == 1,
        regime="fhir-r5-original-supplied-resource",
    )
    return FhirValueSet._owned(
        payload=payload,
        document_json=document_json,
        includes=includes,
        excludes=excludes,
        entries=entries,
        report=report,
    )
