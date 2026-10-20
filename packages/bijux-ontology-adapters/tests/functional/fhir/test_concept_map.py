"""Actual native R5 conditional translation, qualified scopes and visible loss."""

import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from rdflib import URIRef

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.mappings import read_sssom
from bijux_ontology_adapters.fhir.code_system import read_code_system
from bijux_ontology_adapters.fhir.concept_map import (
    FhirConceptMap,
    FhirMapContext,
    FhirMapValue,
    FhirMapValueSet,
    export_concept_map_sssom,
    read_concept_map,
    translate_concept_map,
    write_concept_map,
)
from bijux_ontology_adapters.fhir.identity import (
    FhirCodeBinding,
    FhirCodeIdentity,
)
from bijux_ontology_adapters.fhir.value_set import read_value_set

NATIVE = importlib.util.find_spec("fhir") is not None
native = pytest.mark.skipif(not NATIVE, reason="Actual optional R5 model required")
SOURCE = FhirCodeIdentity("urn:source", "1", "A")


def _map() -> dict[str, Any]:
    """Keep one small valid R5 map with two context routes and a common target."""
    return {
        "resourceType": "ConceptMap",
        "url": "urn:map",
        "version": "1",
        "status": "draft",
        "additionalAttribute": [
            {"code": "purpose", "type": "string"},
            {"code": "note", "type": "string"},
        ],
        "group": [
            {
                "source": "urn:source|1",
                "target": "urn:target|2",
                "element": [
                    {
                        "code": "A",
                        "target": [
                            {
                                "code": "X",
                                "relationship": "equivalent",
                                "dependsOn": [
                                    {"attribute": "purpose", "valueString": "clinical"}
                                ],
                                "product": [
                                    {"attribute": "note", "valueString": "reviewed"}
                                ],
                            },
                            {
                                "code": "Y",
                                "relationship": "source-is-narrower-than-target",
                                "dependsOn": [
                                    {"attribute": "purpose", "valueString": "research"}
                                ],
                            },
                            {"code": "Z", "relationship": "related-to"},
                        ],
                    },
                ],
            }
        ],
    }


def _read(raw: dict[str, Any]) -> FhirConceptMap:
    """Require actual native model and original tree qualification."""
    result = read_concept_map(json.dumps(raw).encode())
    assert result.report.verified, result.report.to_dict()
    return result


def _systems(*, source_case_sensitive: bool = True) -> tuple[Any, ...]:
    """Explicit reversible caller identities; no runtime URI concatenation rule."""
    systems = []
    for system, version, codes in (
        ("urn:source", "1", ("A", "B", "C")),
        ("urn:target", "2", ("X", "Y", "Z", "B", "C")),
    ):
        bindings = tuple(
            FhirCodeBinding(
                FhirCodeIdentity(system, version, code),
                URIRef("urn:bound:" + system[4:] + ":" + code),
            )
            for code in codes
        )
        result = read_code_system(
            json.dumps(
                {
                    "resourceType": "CodeSystem",
                    "status": "active",
                    "url": system,
                    "version": version,
                    "content": "complete",
                    "caseSensitive": source_case_sensitive
                    if system == "urn:source"
                    else True,
                    "concept": [{"code": c} for c in codes],
                }
            ).encode(),
            bindings=bindings,
            case_comparison="ascii" if not source_case_sensitive else None,
        )
        assert result.report.verified, result.report.to_dict()
        systems.append(result)
    return tuple(systems)


def _context(value: str) -> tuple[FhirMapContext, ...]:
    """Keep explicit native string type rather than guessing a code interpretation."""
    return (FhirMapContext("purpose", FhirMapValue("string", json.dumps(value))),)


@native
def test_native_roundtrip_routes_all_contextual_targets_and_products() -> None:
    """The same source has different valid routes; no result is applied."""
    raw = _map()
    owned = _read(raw)
    produced = write_concept_map(owned)
    assert produced.report.verified and produced.content
    assert json.loads(produced.content) == raw
    again = read_concept_map(produced.content)
    assert again.groups == owned.groups and again.attributes == owned.attributes
    systems = _systems()
    clinical = translate_concept_map(
        owned, SOURCE, context=_context("clinical"), systems=systems
    )
    research = translate_concept_map(
        owned, SOURCE, context=_context("research"), systems=systems
    )
    assert clinical.successful and research.successful
    assert [m.target.code for m in clinical.matches if m.target] == ["X", "Z"]
    assert [m.target.code for m in research.matches if m.target] == ["Y", "Z"]
    assert clinical.matches[0].target == FhirCodeIdentity("urn:target", "2", "X")
    assert research.matches[0].relationship == "source-is-narrower-than-target"
    assert clinical.matches[0].products[0].value == FhirMapValue("string", '"reviewed"')
    assert (
        clinical.matches[0].conditions
        == owned.groups[0].elements[0].targets[0].conditions
    )


@native
def test_missing_context_and_exact_system_versions_cannot_choose_a_first_target() -> (
    None
):
    """Known matches remain inspectable while required coverage is unknown."""
    owned, systems = _read(_map()), _systems()
    missing = translate_concept_map(owned, SOURCE, systems=systems)
    assert not missing.successful and missing.status == "unresolved"
    assert [m.target.code for m in missing.matches if m.target] == ["Z"]
    assert any(
        i.kind == "unverified" and "dependsOn" in i.message for i in missing.report
    )
    wrong = translate_concept_map(owned, replace(SOURCE, version="2"), systems=systems)
    reverse = translate_concept_map(
        owned, FhirCodeIdentity("urn:target", "2", "X"), systems=systems
    )
    assert not wrong.report.verified and not reverse.report.verified
    mistyped = translate_concept_map(
        owned,
        SOURCE,
        context=(FhirMapContext("purpose", FhirMapValue("boolean", "true")),),
        systems=systems,
    )
    assert mistyped.report.outcome == "input_error"


@native
def test_boolean_code_and_coding_conditions_keep_native_type_and_identity() -> None:
    """False is a value; Coding display never replaces exact system/version/code."""
    raw = _map()
    raw["additionalAttribute"] = [
        {"code": "flag", "type": "boolean"},
        {"code": "site", "type": "Coding"},
        {"code": "kind", "type": "code"},
    ]
    coding = {
        "system": "urn:source",
        "version": "1",
        "code": "B",
        "display": "original",
    }
    raw["group"][0]["element"][0]["target"] = [
        {
            "code": "X",
            "relationship": "equivalent",
            "dependsOn": [
                {"attribute": "flag", "valueBoolean": False},
                {"attribute": "site", "valueCoding": coding},
                {"attribute": "kind", "valueCode": "B"},
            ],
        }
    ]
    owned, systems = _read(raw), _systems()
    context = (
        FhirMapContext("flag", FhirMapValue("boolean", "false")),
        FhirMapContext(
            "site", FhirMapValue("Coding", json.dumps({**coding, "display": "another"}))
        ),
        FhirMapContext("kind", FhirMapValue("code", '"B"')),
    )
    result = translate_concept_map(owned, SOURCE, context=context, systems=systems)
    assert result.successful and len(result.matches[0].conditions) == 3
    changed = (replace(context[0], value=FhirMapValue("boolean", "true")), *context[1:])
    assert not translate_concept_map(
        owned, SOURCE, context=changed, systems=systems
    ).successful
    target = raw["group"][0]["element"][0]["target"][0]
    target["product"] = [{"attribute": "kind", "valueCode": "MISSING"}]
    invalid_product = translate_concept_map(
        _read(raw), SOURCE, context=context, systems=systems
    )
    assert not invalid_product.successful and invalid_product.matches
    assert any(i.kind == "finding" for i in invalid_product.report)
    target["product"][0]["valueCode"] = "X"
    assert translate_concept_map(
        _read(raw), SOURCE, context=context, systems=systems
    ).successful
    target["product"] = [
        {
            "attribute": "site",
            "valueCoding": {"system": "urn:target", "version": "2", "code": "MISSING"},
        }
    ]
    invalid_coding = translate_concept_map(
        _read(raw), SOURCE, context=context, systems=systems
    )
    assert not invalid_coding.successful and invalid_coding.matches
    assert any(i.kind == "finding" for i in invalid_coding.report)
    target["product"][0]["valueCoding"]["code"] = "X"
    assert translate_concept_map(
        _read(raw), SOURCE, context=context, systems=systems
    ).successful


@native
def test_negative_and_no_map_suppress_fallback_and_remain_distinct() -> None:
    """A declared absence and negative relationship never fabricate a default target."""
    raw = _map()
    group = raw["group"][0]
    group["element"] = [
        {"code": "A", "noMap": True},
        {"code": "B", "target": [{"code": "X", "relationship": "not-related-to"}]},
    ]
    group["unmapped"] = {"mode": "fixed", "code": "Z", "relationship": "equivalent"}
    owned, systems = _read(raw), _systems()
    absent = translate_concept_map(owned, SOURCE, systems=systems)
    negative = translate_concept_map(owned, replace(SOURCE, code="B"), systems=systems)
    fallback = translate_concept_map(owned, replace(SOURCE, code="C"), systems=systems)
    assert (
        absent.status == "no_map"
        and absent.report.verified
        and not absent.successful
        and not absent.matches
    )
    assert (
        negative.status == "negative"
        and negative.report.verified
        and not negative.successful
        and not negative.matches
    )
    assert negative.negative_matches[0].relationship == "not-related-to"
    assert (
        fallback.successful
        and fallback.matches[0].fallback
        and fallback.matches[0].target == FhirCodeIdentity("urn:target", "2", "Z")
    )
    group["element"].append(
        {"code": "A", "target": [{"code": "X", "relationship": "equivalent"}]}
    )
    contradictory = translate_concept_map(_read(raw), SOURCE, systems=systems)
    assert (
        not contradictory.successful and contradictory.matches and contradictory.no_map
    )
    assert any(i.kind == "finding" for i in contradictory.report)
    group["element"].pop()
    group["unmapped"] = {"mode": "other-map", "otherMap": "urn:other|1"}
    unsupported = translate_concept_map(
        _read(raw), replace(SOURCE, code="C"), systems=systems
    )
    assert not unsupported.report.verified and unsupported.status == "unresolved"
    group.pop("unmapped")
    incomplete = translate_concept_map(
        _read(raw), replace(SOURCE, code="C"), systems=systems
    )
    assert incomplete.status == "unresolved" and not incomplete.report.verified


def _value_set(
    system: str, version: str, codes: tuple[str, ...], url: str
) -> FhirMapValueSet:
    """Supply actual native source to the existing finite selection compiler."""
    raw = {
        "resourceType": "ValueSet",
        "status": "active",
        "url": url,
        "version": "1",
        "compose": {
            "include": [
                {
                    "system": system,
                    "version": version,
                    "concept": [{"code": c} for c in codes],
                }
            ]
        },
    }
    owned = read_value_set(json.dumps(raw).encode())
    assert owned.report.verified
    return FhirMapValueSet(owned, "compose")


@native
def test_real_local_value_set_scopes_and_missing_required_expansions() -> None:
    """Scopes use actual versioned ValueSet selection, not asserted sets."""
    raw = _map()
    raw["sourceScopeCanonical"] = "urn:source-scope|1"
    raw["targetScopeCanonical"] = "urn:target-scope|1"
    raw["group"][0]["element"] = [
        {
            "valueSet": "urn:source-scope|1",
            "target": [
                {"valueSet": "urn:target-scope|1", "relationship": "equivalent"}
            ],
        }
    ]
    owned, systems = _read(raw), _systems()
    value_sets = (
        _value_set("urn:source", "1", ("A",), "urn:source-scope"),
        _value_set("urn:target", "2", ("X", "Y"), "urn:target-scope"),
    )
    missing = translate_concept_map(owned, SOURCE, systems=systems)
    assert not missing.report.verified
    result = translate_concept_map(
        owned, SOURCE, systems=systems, value_sets=value_sets
    )
    assert result.successful and [
        m.target.code for m in result.matches if m.target
    ] == ["X", "Y"]
    outside = translate_concept_map(
        owned, replace(SOURCE, code="B"), systems=systems, value_sets=value_sets
    )
    assert outside.status == "not_applicable" and not outside.successful
    insensitive = _systems(source_case_sensitive=False)
    lower = translate_concept_map(
        owned,
        replace(SOURCE, code="a"),
        systems=insensitive,
        value_sets=value_sets,
    )
    assert lower.successful and lower.requested.code == "a"
    assert [m.target.code for m in lower.matches if m.target] == ["X", "Y"]
    raw["additionalAttribute"].append({"code": "kind", "type": "code"})
    target = raw["group"][0]["element"][0]["target"][0]
    target["dependsOn"] = [{"attribute": "kind", "valueSet": "urn:source-scope|1"}]
    conditional = translate_concept_map(
        _read(raw),
        SOURCE,
        context=(FhirMapContext("kind", FhirMapValue("code", '"a"')),),
        systems=insensitive,
        value_sets=value_sets,
    )
    assert conditional.successful
    target.pop("dependsOn")
    raw["additionalAttribute"].append({"code": "out", "type": "Coding"})
    target = raw["group"][0]["element"][0]["target"][0]
    target["product"] = [{"attribute": "out", "valueSet": "urn:missing|1"}]
    unresolved_product = translate_concept_map(
        _read(raw), SOURCE, systems=systems, value_sets=value_sets
    )
    assert not unresolved_product.successful and unresolved_product.matches
    target["product"][0]["valueSet"] = "urn:target-scope|1"
    assert translate_concept_map(
        _read(raw), SOURCE, systems=systems, value_sets=value_sets
    ).successful


@native
def test_sssom_direction_and_explicit_loss_policy_use_real_tooling() -> None:
    """Only explicitly representable unconditional relationships enter actual SSSOM."""
    pytest.importorskip("sssom")
    raw = _map()
    raw["group"][0]["element"][0]["target"] = [
        {"code": "X", "relationship": "source-is-narrower-than-target"}
    ]
    systems = _systems()
    bindings = tuple(b for s in systems for b in s.bindings)
    args: dict[str, Any] = {
        "bindings": bindings,
        "prefixes": (("bound", "urn:bound:"), ("system", "urn:")),
        "mapping_set_id": "urn:mapping",
        "mapping_justification": "https://w3id.org/semapv/vocab/ManualMappingCuration",
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
    }
    exported = export_concept_map_sssom(_read(raw), **args)
    assert exported.report.verified and exported.content, exported.report.to_dict()
    native_read = read_sssom(
        exported.content, mapping_set_id="urn:mapping", license=args["license"]
    )
    assert native_read.report.verified and native_read.mapping_set
    row = native_read.mapping_set.rows[0]
    assert row.predicate == "http://www.w3.org/2004/02/skos/core#broadMatch"
    assert dict(row.cells)["subject_source_version"] == "1"
    assert dict(row.cells)["object_source_version"] == "2"
    contextual = _map()
    rejected = export_concept_map_sssom(_read(contextual), **args)
    assert (
        rejected.content is None and rejected.omitted and not rejected.report.verified
    )
    contextual["group"][0]["element"][0]["target"].append(
        {"code": "X", "relationship": "equivalent"}
    )
    partial = export_concept_map_sssom(_read(contextual), loss="partial", **args)
    assert partial.content and partial.omitted and not partial.report.verified


@native
def test_source_invariants_and_cumulative_cut_preserve_paid_scope_failures() -> None:
    """Actual boolean/invariant errors and late limits never manufacture translation."""
    raw = _map()
    malformed = deepcopy(raw)
    malformed["group"][0]["element"][0]["noMap"] = True
    assert (
        read_concept_map(json.dumps(malformed).encode()).report.outcome == "input_error"
    )
    malformed["group"][0]["element"][0] = {"code": "A", "noMap": "true"}
    assert (
        read_concept_map(json.dumps(malformed).encode()).report.outcome == "input_error"
    )
    owned, systems = _read(raw), _systems()
    budget = Budget(Limits(max_states=500))
    budget.consume("states", 499)
    stopped = translate_concept_map(owned, SOURCE, systems=systems, budget=budget)
    assert not stopped.report.verified and any(
        i.kind == "exhausted" for i in stopped.report
    )


def test_core_optional_absence_is_actionable() -> None:
    """Ordinary adapter import never requires the optional native R5 runtime."""
    if NATIVE:
        pytest.skip("This control requires a genuine core installation")
    result = read_concept_map(b'{"resourceType":"ConceptMap","status":"draft"}')
    assert not result.report.verified and any(
        "[fhir]" in i.message for i in result.report
    )
