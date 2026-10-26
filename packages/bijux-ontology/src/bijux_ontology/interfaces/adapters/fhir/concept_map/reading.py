"""Bounded exact original JSON and actual maintained FHIR R5 ConceptMap models."""

import json
import threading

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.values import array_value, object_value, text_value
from bijux_ontology.rdf.identifiers import absolute_iri

from ..admission import (
    NativeFidelityUnavailable,
    NativeVersionUnavailable,
    admit_r5_json,
)
from ..json import UnsupportedJsonNumber, decode_fhir_json
from ..value_set.reading import _source_kinds, _Unsupported
from .types import (
    FhirConceptMap,
    FhirMapCondition,
    FhirMapElement,
    FhirMapGroup,
    FhirMapTarget,
    FhirMapValue,
)

RELATIONSHIPS = frozenset(
    {
        "related-to",
        "equivalent",
        "source-is-narrower-than-target",
        "source-is-broader-than-target",
        "not-related-to",
    }
)
KINDS = ("Code", "Coding", "String", "Boolean", "Quantity")


def reserve(budget: Budget, count: int = 1) -> None:
    """Prepay immutable record/reference publication before doing child work."""
    budget.consume("work", count + 1)
    budget.consume("states", count)
    budget.consume("bytes", 128 + 64 * count)


def encode(value: object, budget: Budget) -> str:
    """Serialize already decoded bounded values after measured workspace payment."""
    from bijux_ontology.execution.protocol import json_bound

    size = json_bound(value, budget)
    budget.consume("bytes", 4 * size)
    budget.consume("work", size)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def obj(value: object) -> dict[str, object]:
    """Require exact source JSON object shape."""
    return object_value(value, location="ConceptMap")


def optional(record: dict[str, object], key: str) -> str | None:
    """Preserve absent text separately from malformed null or native coercion."""
    return text_value(record[key], location=key) if key in record else None


def canonical(value: str, budget: Budget) -> tuple[str, str | None]:
    """Read the R5 canonical separator without selecting a latest version."""
    bound_text(value, budget)
    parts = value.split("|")
    if len(parts) > 2 or any(not part for part in parts) or "#" in parts[0]:
        raise ResourceError(
            "Canonical fragments or ambiguous versions are outside this profile"
        )
    absolute_iri(parts[0])
    return parts[0], parts[1] if len(parts) == 2 else None


def note(
    issues: IssueCollector,
    message: str,
    *,
    path: tuple[str | int, ...] = (),
    finding: bool = False,
) -> None:
    """Locate unresolved or conclusively invalid selected source obligations."""
    issues.append(
        Issue(
            "adapter.fhir_conceptmap_finding"
            if finding
            else "adapter.fhir_conceptmap_unverified",
            message,
            kind="finding" if finding else "unverified",
            details=Location(path=path),
        )
    )


def failure(error: Exception, issues: IssueCollector) -> None:
    """Preserve paid findings when a later bounded/native stage stops."""
    if isinstance(error, LimitExceeded | MemoryError | RecursionError):
        issues.exhausted(str(error) or type(error).__name__)
        return
    try:
        if isinstance(error, ResourceError | UnsupportedJsonNumber | ImportError):
            note(issues, str(error))
        else:
            issues.append(
                Issue(
                    "adapter.fhir_conceptmap_input_error",
                    str(error)[:256],
                    kind="input_error",
                )
            )
    except LimitExceeded as limit:
        issues.exhausted(str(limit))


def native_json(payload: bytes, budget: Budget) -> tuple[dict[str, object], bytes]:
    """Qualify actual R5 production and exact scalar/tree fidelity, offline."""
    if len(payload) > budget.limits.max_bytes:
        raise LimitExceeded("ConceptMap exceeds max_bytes")
    budget.consume("work", 8 * len(payload) + 1)
    budget.consume("bytes", 32 * len(payload) + 256)
    budget.consume("resources")
    raw = obj(decode_fhir_json(payload, budget))
    try:
        _source_kinds(raw, budget)
    except _Unsupported as error:
        raise ResourceError(str(error)) from error
    if raw.get("resourceType") != "ConceptMap":
        raise InputError("Expected an actual R5 ConceptMap")
    for group in array_value(raw.get("group", [])):
        for element in array_value(obj(group).get("element", [])):
            budget.consume("work")
            record = obj(element)
            if "noMap" in record and type(record["noMap"]) is not bool:
                raise InputError("noMap requires an original JSON boolean")
    try:
        produced = admit_r5_json(payload, raw, "ConceptMap", budget)
    except NativeVersionUnavailable as error:
        raise ResourceError("The maintained model must implement FHIR R5") from error
    except NativeFidelityUnavailable as error:
        raise ResourceError(
            "Native R5 production changes original ConceptMap values"
        ) from error
    except ModuleNotFoundError as error:
        raise ResourceError(
            "Install bijux-ontology[fhir] for maintained R5 models"
        ) from error
    budget.consume("bytes", 4 * len(produced))
    budget.consume("work", len(produced) + 1)
    return raw, produced.encode("utf-8")


def _conditions(
    record: dict[str, object], field: str, attributes: dict[str, str], budget: Budget
) -> tuple[FhirMapCondition, ...]:
    """Preserve typed occurrences and require declaration/value choice agreement."""
    result = []
    for item in array_value(record.get(field, [])):
        reserve(budget)
        data = obj(item)
        attribute = text_value(data.get("attribute"), location="attribute")
        if attribute not in attributes:
            raise InputError(
                "Condition/product references an undeclared additionalAttribute"
            )
        values = [kind for kind in KINDS if "value" + kind in data]
        selected = optional(data, "valueSet")
        if len(values) + int(selected is not None) != 1:
            raise InputError(
                "A condition/product requires exactly one typed value or ValueSet"
            )
        value = None
        if values:
            kind = values[0]
            declared = (
                kind[0].lower() + kind[1:]
                if kind not in {"Coding", "Quantity"}
                else kind
            )
            if attributes[attribute] != declared:
                raise InputError(
                    "Additional attribute type disagrees with its value choice"
                )
            raw = data["value" + kind]
            if (
                kind == "Boolean"
                and type(raw) is not bool
                or kind in {"String", "Code"}
                and type(raw) is not str
            ):
                raise InputError("Condition scalar kinds cannot be coerced")
            value = FhirMapValue(declared, encode(raw, budget))
        result.append(
            FhirMapCondition(attribute, value, selected, encode(data, budget))
        )
    return tuple(result)


def read_concept_map(
    content: bytes | str,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> FhirConceptMap:
    """Read actual R5 source without resolving terminology or asserting translation.

    Native Pydantic internals use bounded source/workspace reservations and
    cooperative checks around the call, not an invented instruction/RSS meter.
    Fractional JSON, extensions/modifiers and native-normalized trees remain
    explicitly unqualified rather than losing source semantics.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    payload, document = b"", "{}"
    attributes: dict[str, str] = {}
    groups = []
    source_scope = target_scope = None
    checked = 0
    try:
        if type(content) not in (bytes, str):
            raise InputError("ConceptMap requires original JSON bytes or text")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("ConceptMap exceeds max_bytes")
        if isinstance(content, str):
            bound_text(content, operation)
            operation.consume("bytes", 4 * len(content))
        payload = content.encode("utf-8") if isinstance(content, str) else content
        raw, _ = native_json(payload, operation)
        document = encode(raw, operation)
        source_scope = optional(raw, "sourceScopeCanonical") or optional(
            raw, "sourceScopeUri"
        )
        target_scope = optional(raw, "targetScopeCanonical") or optional(
            raw, "targetScopeUri"
        )
        for scope in (source_scope, target_scope):
            if scope is not None:
                canonical(scope, operation)
        for item in array_value(raw.get("additionalAttribute", [])):
            reserve(operation)
            attr = obj(item)
            attribute_code, kind = (
                text_value(attr.get("code"), location="attribute"),
                text_value(attr.get("type"), location="type"),
            )
            if attribute_code in attributes or kind not in {
                "code",
                "Coding",
                "string",
                "boolean",
                "Quantity",
            }:
                raise InputError(
                    "Additional attributes require distinct codes and actual R5 kinds"
                )
            attributes[attribute_code] = kind
        for gi, item in enumerate(array_value(raw.get("group", []))):
            reserve(operation)
            group = obj(item)
            path: tuple[str | int, ...] = ("group", gi)
            source, target = optional(group, "source"), optional(group, "target")
            for reference in (source, target):
                if reference is not None:
                    canonical(reference, operation)
            elements = []
            for ei, value in enumerate(array_value(group.get("element", []))):
                reserve(operation)
                element = obj(value)
                ep = (*path, "element", ei)
                code, vs = optional(element, "code"), optional(element, "valueSet")
                if (code is None) == (vs is None):
                    raise InputError(
                        "Source element needs exactly one code or ValueSet"
                    )
                no_map = element.get("noMap", False)
                if type(no_map) is not bool:
                    raise InputError("noMap requires an original boolean")
                targets = []
                for ti, candidate in enumerate(array_value(element.get("target", []))):
                    reserve(operation)
                    t = obj(candidate)
                    tc, tv = optional(t, "code"), optional(t, "valueSet")
                    relationship = text_value(
                        t.get("relationship"), location="relationship"
                    )
                    if relationship not in RELATIONSHIPS or (tc is None) == (
                        tv is None
                    ):
                        raise InputError(
                            "R5 target needs one selector and a directed relationship"
                        )
                    if (
                        raw.get("status") != "draft"
                        and relationship
                        in {"not-related-to", "source-is-broader-than-target"}
                        and not t.get("comment")
                    ):
                        raise InputError(
                            "Broader/negative target needs a comment outside draft"
                        )
                    targets.append(
                        FhirMapTarget(
                            tc,
                            tv,
                            relationship,
                            _conditions(t, "dependsOn", attributes, operation),
                            _conditions(t, "product", attributes, operation),
                            encode(t, operation),
                            (*ep, "target", ti),
                        )
                    )
                if no_map and targets:
                    raise InputError("noMap=true forbids target occurrences")
                elements.append(FhirMapElement(code, vs, no_map, tuple(targets), ep))
            if not elements:
                raise InputError("An R5 mapping group requires source elements")
            unmapped = None
            if "unmapped" in group:
                u = obj(group["unmapped"])
                mode = u.get("mode")
                if mode not in {"fixed", "use-source-code", "other-map"}:
                    raise InputError("Unknown R5 unmapped mode")
                if (
                    mode == "fixed"
                    and (("code" in u) == ("valueSet" in u))
                    or mode != "fixed"
                    and any(k in u for k in ("code", "display", "valueSet"))
                ):
                    raise InputError("R5 unmapped selector disagrees with mode")
                if (
                    mode == "other-map"
                    and not u.get("otherMap")
                    or mode != "other-map"
                    and ("otherMap" in u or u.get("relationship") not in RELATIONSHIPS)
                ):
                    raise InputError(
                        "R5 unmapped relationship/otherMap invariants fail"
                    )
                unmapped = encode(u, operation)
            groups.append(FhirMapGroup(source, target, tuple(elements), unmapped, path))
        operation.consume("work", 0)
        checked = 1
    except (
        InputError,
        ValueError,
        TypeError,
        UnicodeError,
        ImportError,
        UnsupportedJsonNumber,
        MemoryError,
        RecursionError,
    ) as error:
        failure(error, issues)
    return FhirConceptMap._owned(
        payload=payload,
        document_json=document,
        attributes=tuple(attributes.items()),
        source_scope=source_scope,
        target_scope=target_scope,
        groups=tuple(groups),
        report=Report(
            "adapter.fhir_concept_map",
            checked,
            tuple(issues),
            requested=1,
            complete=checked == 1,
            regime="fhir-r5-native-conceptmap",
        ),
    )
