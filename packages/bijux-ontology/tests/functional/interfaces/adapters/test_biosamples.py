"""Real draft-07 schema and occurrence-specific BioSamples semantic controls."""

import gzip
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.interfaces.adapters.biosamples import (
    CORE_SHA256,
    CORE_VERSION,
    CharacteristicBinding,
    SampleCheck,
    SchemaResource,
    check_sample,
)
from bijux_ontology.limits import Budget, Limits

pytest.importorskip("jsonschema")

_CORE = gzip.decompress(
    (
        Path(__file__).parents[3] / "fixtures" / "biosamples" / "sample.json.gz"
    ).read_bytes()
)
_URI = "https://www.ebi.ac.uk/biosamples/schemas/core/sample.json"
_TERM = "https://example.org/allowed"
_OTHER = "https://example.org/other"
_DIALECT = "http://json-schema.org/draft-07/schema#"


def _sample() -> dict[str, Any]:
    """Use unchanged native characteristic arrays rather than tabular substitutes."""
    return {
        "name": "sample",
        "domain": "test",
        "release": "2026-01-01T00:00:00Z",
        "characteristics": {
            "organism": [
                {"text": "first", "unit": "count", "ontologyTerms": [_TERM]},
                {"text": "second", "unit": "count", "ontologyTerms": [_TERM]},
            ]
        },
    }


def _check(
    sample: object,
    *,
    checklist: dict[str, Any] | None = None,
    resources: tuple[SchemaResource, ...] = (),
    bindings: tuple[CharacteristicBinding, ...] | None = None,
    vocabulary: Graph | None = None,
    budget: Budget | None = None,
) -> SampleCheck:
    """Supply explicit versioned bytes, policies and real ontology declarations."""
    if checklist is None:
        checklist = {
            "$schema": _DIALECT,
            "type": "object",
            "required": ["characteristics"],
            "properties": {
                "characteristics": {
                    "required": ["organism"],
                    "description": "terms from PATO are prose, never inferred roots",
                }
            },
        }
    graph = Graph() if vocabulary is None else vocabulary
    if vocabulary is None:
        for term in (_TERM, _OTHER):
            graph.add((URIRef(term), RDF.type, OWL.Class))
    return check_sample(
        sample,
        core=SchemaResource(_URI, CORE_VERSION, _CORE),
        checklist=SchemaResource(
            "https://example.org/checklists/sample.json",
            "1.0",
            json.dumps(checklist).encode(),
        ),
        resources=resources,
        bindings=bindings
        if bindings is not None
        else (
            CharacteristicBinding(
                "organism",
                terms=ValueEnumeration((URIRef(_TERM),)),
                units=ValueEnumeration((Literal("count"),)),
                kinds=(str(OWL.Class),),
                require_text=True,
            ),
        ),
        vocabulary=graph,
        budget=budget,
    )


def test_native_shape_preserves_rows_and_exact_semantic_mutations() -> None:
    """Native-valid disallowed and undeclared IRIs fail at the actual second row."""
    sample = _sample()
    before = deepcopy(sample)
    result = _check(sample)
    assert result.report.verified, result.report.to_dict()
    assert result.resources[0][2] == CORE_SHA256
    assert result.resources[0][1] == CORE_VERSION
    assert [(row.text, row.unit, row.ontology_terms) for row in result.occurrences] == [
        ("first", "count", (_TERM,)),
        ("second", "count", (_TERM,)),
    ]
    assert sample == before
    sample["characteristics"]["organism"][1]["ontologyTerms"] = [_OTHER]
    disallowed = _check(sample)
    assert all(report.verified for report in disallowed.structural_reports)
    assert not disallowed.report.verified
    assert any(
        issue.code == "biosamples.value_disallowed"
        and issue.location == "/characteristics/organism/1/ontologyTerms/0"
        for issue in disallowed.report.issues
    )
    sample["characteristics"]["organism"][1]["ontologyTerms"] = [
        "https://example.org/missing"
    ]
    missing = _check(sample)
    assert any(
        issue.code == "vocabulary.term_missing"
        and issue.location == "/characteristics/organism/1/ontologyTerms/0"
        for issue in missing.report.issues
    )


def test_local_versioned_reference_and_unavailable_contracts_fail_closed() -> None:
    """Only supplied resources resolve; unsupported assertions cannot disappear."""
    sidecar_uri = "https://example.org/checklists/characteristic.json"
    sidecar = SchemaResource(
        sidecar_uri,
        "2026-01",
        json.dumps(
            {
                "$schema": _DIALECT,
                "type": "object",
                "required": ["text", "unit", "ontologyTerms"],
            }
        ).encode(),
    )
    checklist = {
        "$schema": _DIALECT,
        "properties": {
            "characteristics": {
                "properties": {"organism": {"items": {"$ref": "characteristic.json"}}}
            }
        },
    }
    assert _check(_sample(), checklist=checklist, resources=(sidecar,)).report.verified
    missing = _check(_sample(), checklist=checklist)
    assert not missing.report.verified
    assert missing.report.outcome == "unverified"
    extra: dict[str, Any]
    for extra in (
        {"unevaluatedProperties": False},
        {"pattern": "(a+)+$"},
        {"$defs": {}},
    ):
        unknown = _check(_sample(), checklist={"$schema": _DIALECT, **extra})
        assert unknown.report.outcome == "unverified"
    absent = _check(_sample(), bindings=(CharacteristicBinding("absent"),))
    assert not absent.report.verified
    assert any(
        issue.code == "biosamples.binding_missing" for issue in absent.report.issues
    )


def test_real_core_structure_and_explicit_text_unit_requirements() -> None:
    """Native format/type findings and independently selected cells stay distinct."""
    sample = _sample()
    sample["release"] = "not-a-date"
    bad = _check(sample)
    assert any(
        issue.code == "biosamples.schema_mismatch" for issue in bad.report.issues
    )
    assert not bad.occurrences
    sample = _sample()
    del sample["characteristics"]["organism"][0]["unit"]
    missing = _check(sample)
    assert all(report.verified for report in missing.structural_reports)
    assert any(
        issue.code == "biosamples.value_missing"
        and issue.location == "/characteristics/organism/0/unit"
        for issue in missing.report.issues
    )
    sample = _sample()
    sample["characteristics"]["organism"][0]["text"] = 3
    assert not _check(sample).report.verified
    assert not _check(_sample(), bindings=()).report.verified


def test_cumulative_limits_preserve_completed_negative_findings() -> None:
    """A later budget stop retains actual earlier semantic failures."""
    sample = _sample()
    sample["characteristics"]["organism"][0]["ontologyTerms"] = [_OTHER]
    completed = _check(sample)
    assert completed.report.outcome == "disagreement"
    retained = 0
    for work in (100, 10000, 50000, 54000, 54500, 55000, 55100, 56000):
        budget = Budget(Limits(max_work=work))
        result = _check(sample, budget=budget)
        assert not result.report.verified
        assert all(
            budget.remaining(kind) >= 0
            for kind in ("work", "bytes", "states", "issues")
        )
        if any(
            issue.code == "biosamples.value_disallowed"
            for issue in result.report.issues
        ):
            retained += result.report.outcome == "exhausted"
    assert retained
