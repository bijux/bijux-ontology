"""Real R5 producer interchange, qualified membership and dangerous shortcuts."""

import json
from copy import deepcopy
from typing import cast

import pytest
from rdflib import URIRef
from rdflib.namespace import RDFS

from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.adapters.fhir.code_system import (
    check_code_membership,
    read_code_system,
    write_code_system,
)
from bijux_ontology.interfaces.adapters.fhir.identity import (
    FhirCodeBinding,
    FhirCodeIdentity,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.annotations.coverage import check_annotation_coverage
from bijux_ontology.vocabulary.annotations.types import (
    AnnotationRequirement,
    LanguageRequirement,
)
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

CodeSystem = pytest.importorskip("fhir.resources.codesystem").CodeSystem


def _produced(**changes: object) -> str:
    """Obtain input from an actual normally installed maintained R5 producer."""
    document = {
        "resourceType": "CodeSystem",
        "url": "https://example.invalid/codes",
        "version": "release-A",
        "status": "active",
        "content": "complete",
        "language": "en",
        "caseSensitive": True,
        "hierarchyMeaning": "is-a",
        "property": [
            {
                "code": "inactive",
                "uri": "http://hl7.org/fhir/concept-properties#inactive",
                "type": "boolean",
            },
            {"code": "rank", "type": "integer"},
            {"code": "link", "type": "code"},
            {"code": "note", "type": "string"},
        ],
        "concept": [
            {
                "code": "A",
                "display": "Parent",
                "definition": "Parent definition",
                "designation": [
                    {
                        "language": "de",
                        "value": "Eltern",
                        "use": {"system": "urn:designation-use", "code": "preferred"},
                        "additionalUse": [
                            {"system": "urn:designation-use", "code": "short"}
                        ],
                    },
                    {"value": "Default language"},
                ],
                "property": [
                    {"code": "inactive", "valueBoolean": False},
                    {"code": "rank", "valueInteger": 42},
                    {"code": "link", "valueCode": "a"},
                    {"code": "note", "valueString": "Literal text"},
                ],
                "concept": [
                    {
                        "code": "a",
                        "display": "Child",
                        "designation": [{"language": "fr", "value": "Enfant"}],
                    }
                ],
            }
        ],
    }
    document.update(changes)
    return str(CodeSystem.model_validate(document, strict=True).model_dump_json())


def _bindings(source: str) -> tuple[FhirCodeBinding, ...]:
    raw = json.loads(source)
    system, version = raw["url"], raw.get("version")
    if raw["content"] == "supplement":
        parts = raw["supplements"].split("|")
        system, version = parts[0], parts[1] if len(parts) == 2 else None
    stack = list(reversed(raw.get("concept", [])))
    result: list[FhirCodeBinding] = []
    while stack:
        concept = stack.pop()
        result.append(
            FhirCodeBinding(
                FhirCodeIdentity(system, version, concept["code"]),
                URIRef(f"urn:explicit:concept:{len(result)}"),
            )
        )
        stack.extend(reversed(concept.get("concept", [])))
    return tuple(result)


def test_native_roundtrip_exposes_owned_vocabulary_and_exact_records() -> None:
    source = _produced()
    system = read_code_system(source, bindings=_bindings(source))
    assert system.report.verified
    assert [item.identity.code for item in system.concepts] == ["A", "a"]
    parent, child = system.concepts
    assert child.parent == parent.identity and child.iri != parent.iri
    assert parent.designations[1].language is None
    assert parent.designations[1].effective_language == "en"
    assert parent.properties[0].value is False
    assert (
        system.property_definitions[0].uri
        == "http://hl7.org/fhir/concept-properties#inactive"
    )
    assert system.vocabulary is not None
    view = VocabularyView(system.vocabulary)
    coverage = check_annotation_coverage(
        view,
        (
            AnnotationRequirement(
                str(RDFS.label),
                languages=(LanguageRequirement("en"), LanguageRequirement("de")),
            ),
        ),
        entities=(str(parent.iri),),
    )
    assert coverage.report.verified
    missing = check_annotation_coverage(
        view,
        (
            AnnotationRequirement(
                str(RDFS.label), languages=(LanguageRequirement("de"),)
            ),
        ),
        entities=(str(child.iri),),
    )
    assert not missing.report.verified
    output = write_code_system(system)
    assert output.report.verified and output.content is not None
    reparsed = CodeSystem.model_validate_json(output.content, strict=True)
    assert json.loads(reparsed.model_dump_json()) == json.loads(source)
    assert system.enumeration.qualified


def test_identity_mapping_rejects_collisions_missing_and_wrong_version() -> None:
    source = _produced()
    bindings = _bindings(source)
    assert (
        read_code_system(source, bindings=bindings[:1]).report.outcome == "input_error"
    )
    collision = (bindings[0], FhirCodeBinding(bindings[1].identity, bindings[0].iri))
    assert read_code_system(source, bindings=collision).report.outcome == "input_error"
    wrong = (
        FhirCodeBinding(
            FhirCodeIdentity(bindings[0].identity.system, None, "A"), bindings[0].iri
        ),
        bindings[1],
    )
    assert read_code_system(source, bindings=wrong).report.outcome == "input_error"
    second = _produced(url="https://example.invalid/another")
    assert read_code_system(second, bindings=bindings).report.outcome == "input_error"
    second_bindings = tuple(
        FhirCodeBinding(item.identity, URIRef(f"urn:another:concept:{index}"))
        for index, item in enumerate(_bindings(second))
    )
    first_system = read_code_system(source, bindings=bindings + second_bindings)
    second_system = read_code_system(second, bindings=bindings + second_bindings)
    assert first_system.report.verified and second_system.report.verified
    assert (
        first_system.concepts[0].identity.code
        == second_system.concepts[0].identity.code
    )
    assert first_system.concepts[0].identity != second_system.concepts[0].identity
    assert first_system.concepts[0].iri != second_system.concepts[0].iri
    # The combined explicit inventory must keep separate systems' code IRIs.
    aliases = (
        bindings[0],
        FhirCodeBinding(
            FhirCodeIdentity("urn:other-system", "release-A", "A"), bindings[0].iri
        ),
        bindings[1],
    )
    assert read_code_system(source, bindings=aliases).report.outcome == "input_error"


def test_declared_case_regime_preserves_original_spelling_and_unknowns() -> None:
    source = _produced()
    sensitive = read_code_system(source, bindings=_bindings(source))
    a = check_code_membership(
        sensitive, FhirCodeIdentity(sensitive.system, sensitive.version, "a")
    )
    assert a.status == "found" and a.matched == sensitive.concepts[1].identity
    raw = json.loads(source)
    raw["concept"][0].pop("concept")
    raw["caseSensitive"] = False
    source = CodeSystem.model_validate(raw, strict=True).model_dump_json()
    insensitive = read_code_system(
        source, bindings=_bindings(source), case_comparison="ascii"
    )
    a = check_code_membership(
        insensitive, FhirCodeIdentity(insensitive.system, insensitive.version, "a")
    )
    assert a.status == "found" and a.matched is not None and a.matched.code == "A"
    assert a.matched_concept == insensitive.concepts[0]
    assert insensitive.concepts[0].identity.code == "A"
    unspecified = read_code_system(source, bindings=_bindings(source))
    assert (
        check_code_membership(
            unspecified, FhirCodeIdentity(unspecified.system, unspecified.version, "a")
        ).status
        == "unknown"
    )
    assert (
        check_code_membership(
            insensitive, FhirCodeIdentity(insensitive.system, insensitive.version, "ä")
        ).status
        == "unknown"
    )
    raw.pop("caseSensitive")
    source = CodeSystem.model_validate(raw, strict=True).model_dump_json()
    unspecified = read_code_system(source, bindings=_bindings(source))
    assert (
        check_code_membership(
            unspecified,
            FhirCodeIdentity(unspecified.system, unspecified.version, "missing"),
        ).status
        == "unknown"
    )
    colliding = _produced(caseSensitive=False)
    ambiguous = read_code_system(
        colliding, bindings=_bindings(colliding), case_comparison="ascii"
    )
    assert (
        check_code_membership(
            ambiguous, FhirCodeIdentity(ambiguous.system, ambiguous.version, "a")
        ).status
        == "unknown"
    )


def test_content_scope_and_hierarchy_do_not_invent_membership_or_edges() -> None:
    for state in ("complete", "fragment", "supplement"):
        changes = {"content": state, "hierarchyMeaning": "part-of"}
        if state == "supplement":
            changes["supplements"] = "https://example.invalid/base|base-release"
        source = _produced(**changes)
        system = read_code_system(source, bindings=_bindings(source))
        assert system.report.verified
        assert system.hierarchy_meaning == "part-of"
        result = check_code_membership(
            system, FhirCodeIdentity(system.system, system.version, "missing")
        )
        assert result.status == ("absent" if state == "complete" else "unknown")
        assert system.enumeration.qualified is (state == "complete")
        if state == "supplement":
            assert system.vocabulary is None and not system.enumeration.values
            assert system.concepts[0].identity == FhirCodeIdentity(
                "https://example.invalid/base", "base-release", "A"
            )
        else:
            assert system.vocabulary is not None
            description = VocabularyView(system.vocabulary).describe(
                str(system.concepts[1].iri)
            )
            assert all(
                edge.predicate
                not in (
                    "http://www.w3.org/2000/01/rdf-schema#subClassOf",
                    "http://www.w3.org/2004/02/skos/core#broader",
                )
                for edge in description.relationships
            )
        output = write_code_system(system)
        assert output.report.verified and output.content is not None
        assert json.loads(output.content) == json.loads(source)


def test_original_kind_and_cross_record_gaps_do_not_pass_native_acceptance() -> None:
    source = _produced()
    for defect in (
        "boolean",
        "enum",
        "duplicate",
        "supplement",
        "additionalUse",
        "property",
        "count",
    ):
        raw = deepcopy(json.loads(source))
        if defect == "boolean":
            raw["caseSensitive"] = "false"
        elif defect == "enum":
            raw["content"] = "not-a-defined-code"
        elif defect == "duplicate":
            raw["concept"][0]["concept"][0]["code"] = "A"
        elif defect == "supplement":
            raw["content"] = "supplement"
        elif defect == "additionalUse":
            raw["concept"][0]["designation"][0].pop("use")
        elif defect == "property":
            raw["concept"][0]["property"][0] = {
                "code": "inactive",
                "valueString": "false",
            }
        else:
            raw["count"] = 200
        bad = json.dumps(raw)
        CodeSystem.model_validate_json(bad, strict=True)  # Actual native invariant gap.
        result = read_code_system(bad, bindings=_bindings(source))
        assert result.report.outcome == "input_error", defect
        assert not result.enumeration.qualified
        assert write_code_system(result).content is None


def test_unsupported_native_values_and_exhaustion_remain_required_unknown() -> None:
    source = _produced()
    for kind, key, value in (
        ("decimal", "valueDecimal", 0.1),
        ("dateTime", "valueDateTime", "2026-01-01"),
    ):
        raw = json.loads(source)
        raw["property"][0]["type"] = kind
        raw["concept"][0]["property"][0] = {"code": "inactive", key: value}
        bad = CodeSystem.model_validate(raw, strict=True).model_dump_json()
        result = read_code_system(bad, bindings=_bindings(source))
        assert result.report.outcome == "unverified"
        assert write_code_system(result).content is None
    exhausted = read_code_system(
        source, bindings=_bindings(source), limits=Limits(max_work=1)
    )
    assert (
        exhausted.report.outcome == "exhausted" and not exhausted.enumeration.qualified
    )
    system = read_code_system(source, bindings=_bindings(source))
    assert write_code_system(system, limits=Limits(max_work=1)).content is None
    membership = check_code_membership(
        system, system.concepts[0].identity, limits=Limits(max_work=1)
    )
    assert membership.status == "unknown" and membership.report.outcome == "exhausted"


def test_nested_native_coercion_and_source_bounds_cannot_launder_fidelity() -> None:
    source = _produced()
    raw = json.loads(source)
    raw["useContext"] = [
        {
            "code": {"system": "urn:context", "code": "age"},
            "valueQuantity": {"value": 0.0, "unit": "years"},
        }
    ]
    fractional = json.dumps(raw).replace(
        '"value": 0.0', '"value": 0.123456789012345678901234567890'
    )
    native = CodeSystem.model_validate_json(fractional, strict=True)
    assert "0.12345678901234568" in native.model_dump_json()
    assert (
        read_code_system(fractional, bindings=_bindings(source)).report.outcome
        == "unverified"
    )
    raw = json.loads(source)
    raw["meta"] = {
        "security": [{"system": "urn:security", "code": "x", "userSelected": 1}]
    }
    coercion = json.dumps(raw)
    native = CodeSystem.model_validate_json(coercion, strict=True)
    assert (
        json.loads(native.model_dump_json())["meta"]["security"][0]["userSelected"]
        is True
    )
    assert (
        read_code_system(coercion, bindings=_bindings(source)).report.outcome
        == "unverified"
    )
    assert (
        read_code_system(
            source, bindings=_bindings(source), limits=Limits(max_bytes=1)
        ).report.outcome
        == "exhausted"
    )
    assert (
        read_code_system(
            source, bindings=_bindings(source), limits=Limits(max_total_bytes=1)
        ).report.outcome
        == "exhausted"
    )
    long = _produced(concept=[{"code": "A" * 300}])
    system = read_code_system(long, bindings=_bindings(long))
    assert system.report.verified
    result = check_code_membership(
        system,
        FhirCodeIdentity(system.system, system.version, "B"),
        limits=Limits(max_bytes=100),
    )
    assert result.status == "unknown" and result.report.outcome == "exhausted"


def test_current_budget_owns_captured_payload_and_adopted_child_reports() -> None:
    """Rejected source capture/report adoption cannot reappear in returned evidence."""
    source = _produced()
    refused = read_code_system(
        source.encode(), bindings=_bindings(source), limits=Limits(max_total_bytes=100)
    )
    assert refused.report.outcome == "exhausted" and refused.payload == b""
    unsupported_source = _produced(compositional=True)
    unsupported = read_code_system(
        unsupported_source, bindings=_bindings(unsupported_source)
    )
    assert unsupported.report.outcome == "unverified"
    identity = FhirCodeIdentity("urn:codes", None, "A")
    membership = check_code_membership(
        unsupported, identity, limits=Limits(max_bytes=30)
    )
    exported = write_code_system(unsupported, limits=Limits(max_bytes=30))
    for report in (membership.report, exported.report):
        assert report.outcome == "exhausted" and not report.children
        assert all(issue.kind == "exhausted" for issue in report.issues)
    with pytest.raises(InputError):
        read_code_system(
            source, bindings=_bindings(source), budget=cast(Budget, object())
        )
    with pytest.raises(InputError):
        check_code_membership(unsupported, identity, budget=cast(Budget, object()))
    with pytest.raises(InputError):
        write_code_system(unsupported, budget=cast(Budget, object()))
