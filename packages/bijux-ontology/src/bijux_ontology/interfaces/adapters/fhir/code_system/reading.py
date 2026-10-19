"""Bounded original R5 JSON, maintained model parsing and explicit RDF identity."""

import json
from importlib import import_module
from typing import Literal as Choice
from typing import cast

from rdflib import Graph, Literal
from rdflib.namespace import RDF, RDFS, SKOS

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

from ..identity import FhirCodeBinding, FhirCodeIdentity, capture_code_bindings
from ..json import UnsupportedJsonNumber, decode_fhir_json, same_json
from .types import (
    FhirCodeSystem,
    FhirConcept,
    FhirDesignation,
    FhirProperty,
    FhirPropertyDefinition,
)

_KINDS = {
    "code": "valueCode",
    "string": "valueString",
    "integer": "valueInteger",
    "boolean": "valueBoolean",
}
_UNSUPPORTED = {
    "extension",
    "modifierExtension",
    "implicitRules",
    "contained",
    "filter",
    "versionAlgorithmString",
    "versionAlgorithmCoding",
}


class _Unsupported(Exception):
    """A real R5 feature outside the qualified interpretation boundary."""


def _operation(limits: Limits | None, budget: Budget | None) -> Budget:
    """Reuse the caller's cumulative allowance without conflicting limits."""
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("FHIR limits require Limits")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("FHIR budget requires Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("FHIR limits conflict with the shared budget")
    return budget if budget is not None else Budget(limits)


def _object(value: object) -> dict[str, object]:
    """Keep maintained JSON decoding separate from resource interpretation."""
    if not isinstance(value, dict):
        raise InputError("FHIR records require JSON objects")
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    """Reject scalar-to-list coercion in repeated interpreted fields."""
    if not isinstance(value, list):
        raise InputError("FHIR repeated fields require JSON arrays")
    return value


def _text(value: object) -> str:
    """Require the original primitive kind before maintained model coercion."""
    if type(value) is not str or not value:
        raise InputError("FHIR interpreted text requires a nonempty JSON string")
    return value


def _optional(record: dict[str, object], key: str) -> str | None:
    """Preserve absent text distinctly from an invalid explicit null."""
    return _text(record[key]) if key in record else None


def _supported(record: dict[str, object], allowed: set[str] | None = None) -> None:
    """Qualify interpreted feature ownership rather than pretend full validation."""
    if (
        _UNSUPPORTED.intersection(record)
        or allowed is not None
        and set(record) - allowed
    ):
        raise _Unsupported("Source contains fields outside the supported R5 profile")


def _encode(value: object) -> str:
    """Retain bounded JSON records without changing source scalar values."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _coding(value: object) -> str:
    """Keep designation Coding qualifiers whole without terminology inference."""
    record = _object(value)
    _supported(record, {"system", "version", "code", "display", "userSelected"})
    for key in ("system", "version", "code", "display"):
        if key in record:
            _text(record[key])
    if "system" in record:
        absolute_iri(_text(record["system"]))
    if "userSelected" in record and type(record["userSelected"]) is not bool:
        raise InputError("Coding userSelected requires a JSON boolean")
    return _encode(record)


def _definitions(
    raw: dict[str, object], budget: Budget
) -> tuple[FhirPropertyDefinition, ...]:
    """Interpret only declared supported property kinds and exact meanings."""
    definitions = []
    seen = set()
    for value in _array(raw.get("property", [])):
        budget.consume("work")
        budget.consume("states")
        record = _object(value)
        _supported(record, {"code", "uri", "description", "type"})
        code, kind = _text(record.get("code")), _text(record.get("type"))
        if code in seen:
            raise InputError("CodeSystem property definitions must have distinct codes")
        seen.add(code)
        if kind not in _KINDS:
            raise _Unsupported("Property kind is outside code/string/integer/boolean")
        uri = _optional(record, "uri")
        if uri is not None:
            absolute_iri(uri)
        definitions.append(
            FhirPropertyDefinition(code, uri, kind, _optional(record, "description"))
        )
    return tuple(definitions)


def _designations(
    raw: dict[str, object], language: str | None, budget: Budget
) -> tuple[FhirDesignation, ...]:
    """Apply only the resource-language fallback defined by R5."""
    designations = []
    for value in _array(raw.get("designation", [])):
        budget.consume("work")
        budget.consume("states")
        record = _object(value)
        _supported(record, {"language", "use", "additionalUse", "value"})
        original = _optional(record, "language")
        if original is not None:
            language_tag(original)
        use = _coding(record["use"]) if "use" in record else None
        additional = tuple(
            _coding(value) for value in _array(record.get("additionalUse", []))
        )
        if additional and use is None:
            raise InputError("FHIR designation additionalUse requires use")
        designations.append(
            FhirDesignation(
                _text(record.get("value")),
                original,
                original or language,
                use,
                additional,
            )
        )
    return tuple(designations)


def _properties(
    raw: dict[str, object],
    definitions: tuple[FhirPropertyDefinition, ...],
    budget: Budget,
) -> tuple[FhirProperty, ...]:
    """Bind each original property occurrence to its declared scalar kind."""
    kinds = {item.code: item.kind for item in definitions}
    properties = []
    for value in _array(raw.get("property", [])):
        budget.consume("work")
        budget.consume("states")
        record = _object(value)
        _supported(
            record,
            {"code", *_KINDS.values(), "valueDecimal", "valueDateTime", "valueCoding"},
        )
        code = _text(record.get("code"))
        keys = set(record) - {"code"}
        if len(keys) != 1:
            raise InputError("A concept property requires exactly one typed value")
        if keys.intersection({"valueDecimal", "valueDateTime", "valueCoding"}):
            raise _Unsupported(
                "Native decimal/dateTime/Coding properties are not qualified"
            )
        if code not in kinds:
            raise _Unsupported("Concept property lacks an interpreted type definition")
        kind = kinds[code]
        if keys != {_KINDS[kind]}:
            raise InputError("Concept property kind conflicts with its declaration")
        item = record[_KINDS[kind]]
        if kind in {"code", "string"}:
            item = _text(item)
        elif type(item) is not (bool if kind == "boolean" else int):
            raise InputError("FHIR properties require original JSON scalar kinds")
        properties.append(FhirProperty(code, kind, item))
    return tuple(properties)


def _concepts(
    raw: dict[str, object],
    system: str,
    version: str | None,
    language: str | None,
    definitions: tuple[FhirPropertyDefinition, ...],
    bindings: tuple[FhirCodeBinding, ...],
    budget: Budget,
) -> tuple[FhirConcept, ...]:
    """Traverse the actual finite hierarchy with caller-owned bijective identity."""
    mapping = {item.identity: item.iri for item in bindings}
    stack: list[tuple[object, FhirCodeIdentity | None]] = [
        (value, None) for value in reversed(_array(raw.get("concept", [])))
    ]
    concepts = []
    seen = set()
    while stack:
        budget.consume("work")
        budget.consume("states")
        value, parent = stack.pop()
        record = _object(value)
        _supported(
            record,
            {"code", "display", "definition", "designation", "property", "concept"},
        )
        code = _text(record.get("code"))
        if code in seen:
            raise InputError("CodeSystem codes must be unique throughout the hierarchy")
        seen.add(code)
        identity = FhirCodeIdentity(system, version, code)
        if identity not in mapping:
            raise InputError(
                "Every CodeSystem concept needs its exact explicit RDF binding"
            )
        concepts.append(
            FhirConcept(
                identity,
                mapping[identity],
                parent,
                _optional(record, "display"),
                _optional(record, "definition"),
                _designations(record, language, budget),
                _properties(record, definitions, budget),
            )
        )
        children = _array(record.get("concept", []))
        stack.extend((child, identity) for child in reversed(children))
    return tuple(concepts)


def _projection(
    concepts: tuple[FhirConcept, ...], language: str | None, budget: Budget
) -> Graph:
    """Expose labels and code declarations without inventing hierarchy semantics."""
    graph = Graph()
    for concept in concepts:
        budget.consume("states", 4 + len(concept.designations))
        triples = [
            (concept.iri, RDF.type, SKOS.Concept),
            (
                concept.iri,
                SKOS.notation,
                Literal(concept.identity.code, normalize=False),
            ),
        ]
        for predicate, value in (
            (RDFS.label, concept.display),
            (SKOS.definition, concept.definition),
        ):
            if value is not None:
                triples.append(
                    (
                        concept.iri,
                        predicate,
                        Literal(value, lang=language, normalize=False),
                    )
                )
        triples.extend(
            (
                concept.iri,
                RDFS.label,
                Literal(item.value, lang=item.effective_language, normalize=False),
            )
            for item in concept.designations
        )
        for triple in triples:
            budget.consume("work")
            budget.consume("triples")
            graph.add(triple)
    return graph


def read_code_system(
    content: bytes | str,
    *,
    bindings: tuple[FhirCodeBinding, ...],
    case_comparison: Choice["ascii"] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FhirCodeSystem:
    """Read a local R5 subset with maintained parsing and exact explicit identities.

    Four property kinds are qualified. Native model acceptance alone does not
    establish FHIR invariants or scalar fidelity. Fragment/supplement membership
    and lexical comparison remain separate from interchange validity.
    """
    operation = _operation(limits, budget)
    issues = IssueCollector(operation)
    payload = b""
    document_json = "{}"
    owned_bindings: tuple[FhirCodeBinding, ...] = ()
    concepts: tuple[FhirConcept, ...] = ()
    definitions: tuple[FhirPropertyDefinition, ...] = ()
    system, state = "", ""
    version = language = supplement = hierarchy = None
    case_sensitive = None
    vocabulary = None
    checked = 0
    try:
        if case_comparison not in (None, "ascii") or type(case_comparison) not in (
            str,
            type(None),
        ):
            raise InputError(
                "Only an explicitly selected ASCII comparison regime is supported"
            )
        if type(content) not in (bytes, str):
            raise InputError("CodeSystem requires supplied JSON bytes or text")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("CodeSystem exceeds max_bytes")
        if isinstance(content, str):
            operation.consume("work", len(content) + 1)
            operation.consume("bytes", 4 * len(content))
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        if len(encoded) > operation.limits.max_bytes:
            raise LimitExceeded("CodeSystem exceeds max_bytes")
        operation.consume("resources")
        operation.consume("bytes", 5 * len(encoded))
        operation.consume("work", 4 * len(encoded) + 1)
        payload = encoded

        def qualify_key(key: str) -> None:
            """Refuse unqualified modifiers even within retained metadata."""
            if key in _UNSUPPORTED or key.startswith("_"):
                raise _Unsupported(
                    "FHIR extensions and unqualified features are outside "
                    "the supported profile"
                )

        raw = _object(decode_fhir_json(payload, operation, key_hook=qualify_key))
        document_json = _encode(raw)
        owned_bindings = capture_code_bindings(bindings, operation)
        if raw.get("resourceType") != "CodeSystem":
            raise InputError("Expected an actual R5 CodeSystem resource")
        _supported(raw)
        system = _text(raw.get("url"))
        absolute_iri(system)
        version, language = _optional(raw, "version"), _optional(raw, "language")
        if language is not None:
            language_tag(language)
        for field in (
            "caseSensitive",
            "compositional",
            "versionNeeded",
            "experimental",
        ):
            if field in raw and type(raw[field]) is not bool:
                raise InputError("CodeSystem flags require original JSON booleans")
        case_sensitive = cast(bool | None, raw.get("caseSensitive"))
        if raw.get("status") not in {"draft", "active", "retired", "unknown"}:
            raise InputError("CodeSystem publication status is invalid")
        state = _text(raw.get("content"))
        if state not in {
            "not-present",
            "example",
            "fragment",
            "complete",
            "supplement",
        }:
            raise InputError("CodeSystem content status is invalid")
        hierarchy = _optional(raw, "hierarchyMeaning")
        if hierarchy is not None and hierarchy not in {
            "grouped-by",
            "is-a",
            "part-of",
            "classified-with",
        }:
            raise InputError("CodeSystem hierarchy meaning is invalid")
        if raw.get("compositional") is True:
            raise _Unsupported(
                "Compositional grammar is outside finite code membership"
            )
        supplement = _optional(raw, "supplements")
        concept_system, concept_version = system, version
        if state == "supplement":
            if supplement is None:
                raise InputError("A supplement requires its target canonical reference")
            parts = supplement.split("|")
            if len(parts) > 2 or any(not part for part in parts):
                raise InputError("Supplement target canonical reference is invalid")
            concept_system, concept_version = (
                parts[0],
                parts[1] if len(parts) == 2 else None,
            )
            absolute_iri(concept_system)
        definitions = _definitions(raw, operation)
        concepts = _concepts(
            raw,
            concept_system,
            concept_version,
            language,
            definitions,
            owned_bindings,
            operation,
        )
        if "count" in raw:
            count = raw["count"]
            if type(count) is not int or count < 0:
                raise InputError(
                    "CodeSystem count requires an original nonnegative integer"
                )
            if state == "complete" and count != len(concepts):
                raise InputError(
                    "Complete CodeSystem count conflicts with its explicit inventory"
                )
        from fhir.resources import __fhir_version__

        if __fhir_version__ != "5.0.0":
            raise _Unsupported("The installed maintained model is not FHIR R5")
        native_type = import_module("fhir.resources.codesystem").CodeSystem
        native = native_type.model_validate_json(payload, strict=True)
        produced = native.model_dump_json()
        if len(produced) > operation.limits.max_bytes or not same_json(
            raw, json.loads(produced), operation
        ):
            raise _Unsupported(
                "Maintained model changes source values outside the qualified profile"
            )
        if state != "supplement":
            vocabulary = load_vocabulary(
                _projection(concepts, language, operation), budget=operation
            )
        checked = 1
    except ModuleNotFoundError:
        try:
            issues.append(
                Issue(
                    "adapter.fhir_backend_unavailable",
                    "Install bijux-ontology[fhir] for the maintained R5 model",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the missing native model")
    except (_Unsupported, UnsupportedJsonNumber) as error:
        try:
            issues.append(
                Issue("adapter.fhir_unsupported", str(error), kind="unverified")
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the unsupported R5 capability")
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, ValueError, TypeError, UnicodeError, RecursionError):
        try:
            issues.append(
                Issue(
                    "adapter.fhir_input_error",
                    "Original R5 source, native model, or explicit RDF bindings "
                    "are invalid",
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining the CodeSystem input failure")
    report = Report(
        "adapter.fhir_code_system",
        checked,
        tuple(issues),
        requested=1,
        complete=not any(item.kind in {"unverified", "exhausted"} for item in issues),
        regime="fhir-r5-explicit-identity",
    )
    if not report.verified:
        vocabulary = None
    enumeration = ValueEnumeration(
        tuple(item.iri for item in concepts)
        if report.verified and state != "supplement"
        else (),
        closed=report.verified and state == "complete",
        report=report,
    )
    return FhirCodeSystem._from_owned(
        payload=payload,
        document_json=document_json,
        system=system,
        version=version,
        language=language,
        case_sensitive=case_sensitive,
        case_comparison=case_comparison,
        content=state,
        supplements=supplement,
        hierarchy_meaning=hierarchy,
        property_definitions=definitions,
        concepts=concepts,
        bindings=owned_bindings,
        vocabulary=vocabulary,
        enumeration=enumeration,
        report=report,
    )
