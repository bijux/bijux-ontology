"""Actual R5 models, finite local selections and ordinary RDF output consumers."""

import json
from typing import cast

import pytest
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.interfaces.adapters.fhir.code_system import (
    FhirCodeSystem,
    read_code_system,
)
from bijux_ontology.interfaces.adapters.fhir.identity import (
    FhirCodeBinding,
    FhirCodeIdentity,
)
from bijux_ontology.interfaces.adapters.fhir.value_set import (
    FhirValueSetPolicy,
    compile_value_set,
    read_value_set,
    write_value_set,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage

pytest.importorskip("fhir.resources.valueset")
EX = Namespace("https://example.org/value-set/")
SYSTEM = str(EX.system)


def _encoded(**fields: object) -> bytes:
    return json.dumps(
        {"resourceType": "ValueSet", "status": "active", **fields}
    ).encode()


def _source(
    system: str = SYSTEM,
    version: str | None = "1",
    *,
    content: str = "complete",
    flags: bool = False,
) -> tuple[FhirCodeSystem, tuple[FhirCodeBinding, ...]]:
    bindings = tuple(
        FhirCodeBinding(
            FhirCodeIdentity(system, version, code), URIRef(system + "/" + code)
        )
        for code in ("A", "B", "C")
    )
    raw: dict[str, object] = {
        "resourceType": "CodeSystem",
        "status": "active",
        "url": system,
        "caseSensitive": True,
        "content": content,
        "concept": [{"code": code} for code in ("A", "B", "C")],
    }
    if version is not None:
        raw["version"] = version
    if flags:
        raw["property"] = [
            {
                "code": "group",
                "uri": "http://hl7.org/fhir/concept-properties#notSelectable",
                "type": "boolean",
            },
            {
                "code": "old",
                "uri": "http://hl7.org/fhir/concept-properties#inactive",
                "type": "boolean",
            },
        ]
        raw["concept"] = [
            {"code": "A", "property": [{"code": "group", "valueBoolean": True}]},
            {"code": "B", "property": [{"code": "old", "valueBoolean": True}]},
            {"code": "C"},
        ]
    result = read_code_system(json.dumps(raw), bindings=bindings)
    assert result.report.verified, result.report.to_dict()
    return result, bindings


def _compose(**extra: object) -> dict[str, object]:
    return {
        "include": [
            {
                "system": SYSTEM,
                "version": "1",
                "concept": [{"code": code} for code in ("A", "B", "C")],
            }
        ],
        "exclude": [{"system": SYSTEM, "version": "1", "concept": [{"code": "B"}]}],
        **extra,
    }


def _entry(
    code: str, *, system: str = SYSTEM, version: str | None = "1", **extra: object
) -> dict[str, object]:
    result: dict[str, object] = {"system": system, "code": code, **extra}
    if version is not None:
        result["version"] = version
    return result


def _expansion(
    contains: list[dict[str, object]], total: int, **extra: object
) -> dict[str, object]:
    return {
        "timestamp": "2026-01-01T00:00:00Z",
        "contains": contains,
        "total": total,
        **extra,
    }


def test_real_compose_difference_drives_checked_rdf_output_and_native_export() -> None:
    system, bindings = _source()
    source = read_value_set(_encoded(compose=_compose()))
    assert source.report.verified
    selected = compile_value_set(
        source, mode="compose", systems=(system,), bindings=bindings
    )
    assert selected.report.verified and selected.enumeration.qualified, (
        selected.report.to_dict()
    )
    assert {identity.code for identity in selected.identities} == {"A", "C"}
    assert {identity.code for identity in selected.excluded} == {"B"}
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "choice",
                str(EX.choice),
                (
                    ValueDeclaration(
                        "iri",
                        constraints=ValueConstraints(enumeration=selected.enumeration),
                    ),
                ),
            ),
        ),
    )
    for binding in bindings:
        graph = (
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.choice, binding.iri))
        )
        report = check_rdf_emission(
            prepare_source(graph), (model,), (RdfModelUsage("record", EX.record),)
        )
        assert report.verified is (binding.identity.code != "B"), report.to_dict()
    exported = write_value_set(source)
    assert exported.report.verified and exported.content is not None
    assert json.loads(exported.content) == json.loads(source.payload)
    assert read_value_set(exported.content).report.verified


def test_abstract_navigation_preserves_tree_and_own_systems_without_inheritance() -> (
    None
):
    system, bindings = _source()
    other, other_bindings = _source(str(EX.other))
    tree = [
        {
            "abstract": True,
            "display": "Navigation",
            "contains": [_entry("A"), _entry("A", system=other.system)],
        }
    ]
    source = read_value_set(
        _encoded(
            expansion=_expansion(
                tree,
                2,
                parameter=[{"name": "includeDesignations", "valueBoolean": True}],
            )
        )
    )
    assert source.report.verified
    group = source.entries[0]
    assert group.identity is None and group.abstract and len(group.children) == 2
    assert group.children[0].path == (0, 0)
    selection = compile_value_set(
        source,
        mode="expansion",
        systems=(system, other),
        bindings=bindings + other_bindings,
    )
    assert selection.enumeration.qualified, selection.report.to_dict()
    assert (
        len(selection.identities) == 2
        and len({item.system for item in selection.identities}) == 2
    )
    native = write_value_set(source)
    assert native.report.verified and native.content is not None
    assert json.loads(native.content) == json.loads(source.payload)
    missing = read_value_set(
        _encoded(
            expansion=_expansion(
                [_entry("A", contains=[{"code": "B", "version": "1"}])], 2
            )
        )
    )
    assert missing.report.outcome == "input_error"
    assert not write_value_set(missing).report.verified


def test_absent_versions_are_exact_under_explicit_consumer_policy() -> None:
    versioned, bindings = _source()
    source = read_value_set(
        _encoded(expansion=_expansion([_entry("A", version=None)], 1))
    )
    assert source.report.verified and source.entries[0].identity is not None
    assert source.entries[0].identity.version is None
    default = compile_value_set(
        source, mode="expansion", systems=(versioned,), bindings=bindings
    )
    assert not default.enumeration.qualified and not default.identities
    unversioned, exact = _source(version=None)
    explicit = compile_value_set(
        source,
        mode="expansion",
        systems=(unversioned,),
        bindings=exact,
        policy=FhirValueSetPolicy(allow_unversioned=True),
    )
    assert explicit.enumeration.qualified and explicit.identities[0].version is None
    never_fill = compile_value_set(
        source,
        mode="expansion",
        systems=(versioned,),
        bindings=bindings,
        policy=FhirValueSetPolicy(allow_unversioned=True),
    )
    assert not never_fill.enumeration.qualified


def test_pages_unavailable_versions_and_open_exclusions_never_close() -> None:
    system, bindings = _source()
    for extras in (
        {"offset": 0},
        {"next": "http://127.0.0.1:9/never-fetch"},
        {"parameter": [{"name": "filter", "valueString": "A"}]},
        {"total": 8},
    ):
        expansion = _expansion([_entry("A")], 1)
        expansion.update(extras)
        source = read_value_set(_encoded(expansion=expansion))
        selected = compile_value_set(
            source, mode="expansion", systems=(system,), bindings=bindings
        )
        assert not selected.enumeration.qualified and not selected.enumeration.closed
        assert write_value_set(source).report.verified
    for clause in (
        {"system": SYSTEM, "version": "missing", "concept": [{"code": "A"}]},
        {
            "system": SYSTEM,
            "version": "1",
            "filter": [{"property": "concept", "op": "is-a", "value": "A"}],
        },
        {"valueSet": ["http://127.0.0.1:9/never-fetch"]},
    ):
        source = read_value_set(_encoded(compose={"include": [clause]}))
        assert source.report.verified, source.report.to_dict()
        selected = compile_value_set(
            source, mode="compose", systems=(system,), bindings=bindings
        )
        assert not selected.enumeration.qualified
    compose = _compose()
    cast(list[object], compose["exclude"]).append(
        {
            "system": SYSTEM,
            "version": "1",
            "filter": [{"property": "concept", "op": "is-a", "value": "C"}],
        }
    )
    selected = compile_value_set(
        read_value_set(_encoded(compose=compose)),
        mode="compose",
        systems=(system,),
        bindings=bindings,
    )
    assert not selected.enumeration.qualified and not selected.identities
    assert {identity.code for identity in selected.excluded} == {"B"}
    fragment, mapping = _source(content="fragment")
    whole = read_value_set(
        _encoded(compose={"include": [{"system": SYSTEM, "version": "1"}]})
    )
    assert not compile_value_set(
        whole, mode="compose", systems=(fragment,), bindings=mapping
    ).enumeration.qualified


def test_abstract_duplicates_and_real_property_meanings_remain_qualified() -> None:
    system, bindings = _source(flags=True)
    source = read_value_set(
        _encoded(compose={"include": [{"system": SYSTEM, "version": "1"}]})
    )
    selected = compile_value_set(
        source, mode="compose", systems=(system,), bindings=bindings
    )
    assert selected.enumeration.qualified and [
        item.code for item in selected.identities
    ] == ["C"]
    allow = compile_value_set(
        source,
        mode="compose",
        systems=(system,),
        bindings=bindings,
        policy=FhirValueSetPolicy(include_inactive=True),
    )
    assert allow.enumeration.qualified and {item.code for item in allow.identities} == {
        "B",
        "C",
    }
    inactive = read_value_set(
        _encoded(
            compose={"inactive": False, "include": [{"system": SYSTEM, "version": "1"}]}
        )
    )
    forced = compile_value_set(
        inactive,
        mode="compose",
        systems=(system,),
        bindings=bindings,
        policy=FhirValueSetPolicy(include_inactive=True),
    )
    assert [item.code for item in forced.identities] == ["C"]
    ordinary, mapping = _source()
    for contains in (
        [_entry("A", abstract=True, contains=[_entry("C")])],
        [_entry("C"), _entry("C")],
    ):
        source = read_value_set(_encoded(expansion=_expansion(contains, len(contains))))
        selected = compile_value_set(
            source, mode="expansion", systems=(ordinary,), bindings=mapping
        )
        assert not selected.enumeration.qualified
        assert {item.code for item in selected.identities} == {"C"}
        assert all(item.code != "A" for item in selected.identities)
    supplied = read_value_set(
        _encoded(
            expansion=_expansion(
                [_entry("C", property=[{"code": "group", "valueBoolean": True}])],
                1,
                property=[
                    {
                        "code": "group",
                        "uri": "http://hl7.org/fhir/concept-properties#notSelectable",
                    }
                ],
            )
        )
    )
    marked = compile_value_set(
        supplied, mode="expansion", systems=(ordinary,), bindings=mapping
    )
    assert not marked.identities
    assert {identity.code for identity in marked.excluded} == {"C"}


def test_native_coercion_and_unsupported_decimal_do_not_certify_source() -> None:
    from fhir.resources.valueset import ValueSet

    for item in (
        {"code": "A", "system": SYSTEM, "abstract": "false"},
        {"display": "Group"},
        {},
    ):
        source = read_value_set(
            _encoded(expansion=_expansion([cast(dict[str, object], item)], 1))
        )
        assert source.report.outcome == "input_error"
    decimal = read_value_set(
        b'{"resourceType":"ValueSet","status":"active","expansion":{"timestamp":"2026-01-01T00:00:00Z","parameter":[{"name":"precision","valueDecimal":0.12345678901234567890123456789}]}}'
    )
    assert not decimal.report.verified and decimal.payload
    assert write_value_set(decimal).content is None
    nested = _encoded(
        useContext=[
            {
                "code": {"system": "https://example.org/context", "code": "age"},
                "valueQuantity": {"value": "DECIMAL"},
            }
        ]
    ).replace(b'"DECIMAL"', b"0.12345678901234567890123456789")
    assert ValueSet.model_validate_json(nested, strict=True).useContext
    captured = read_value_set(nested)
    assert captured.payload == nested and captured.report.outcome == "unverified"
    assert write_value_set(captured).content is None
    boolean = _encoded(
        topic=[
            {
                "coding": [
                    {
                        "system": "https://example.org/topic",
                        "code": "x",
                        "userSelected": 1,
                    }
                ]
            }
        ]
    )
    produced = ValueSet.model_validate_json(boolean, strict=True).model_dump_json()
    assert json.loads(produced)["topic"][0]["coding"][0]["userSelected"] is True
    changed = read_value_set(boolean)
    assert changed.report.outcome == "unverified"
    assert write_value_set(changed).content is None


def test_cumulative_budget_stops_publication_and_retains_known_input_findings() -> None:
    system, bindings = _source()
    payload = _encoded(compose=_compose())
    budget = Budget()
    source = read_value_set(payload, budget=budget)
    selected = compile_value_set(
        source, mode="compose", systems=(system,), bindings=bindings, budget=budget
    )
    assert selected.enumeration.qualified
    assert budget.remaining("work") < Limits().max_work - len(payload)
    limited = Budget(Limits(max_work=700))
    captured = read_value_set(payload, budget=limited)
    failed = compile_value_set(
        captured, mode="compose", systems=(system,), bindings=bindings, budget=limited
    )
    assert not failed.enumeration.qualified
    assert limited.remaining("work") >= 0
    text = json.dumps(
        {"resourceType": "ValueSet", "status": "active", "description": "é" * 1000},
        ensure_ascii=False,
    )
    unpaid = read_value_set(text, limits=Limits(max_total_bytes=100))
    assert unpaid.report.outcome == "exhausted" and unpaid.payload == b""
    with pytest.raises(InputError):
        read_value_set(payload, budget=cast(Budget, object()))
    with pytest.raises(InputError):
        write_value_set(source, limits=cast(Limits, object()))
    with pytest.raises(InputError):
        compile_value_set(
            source,
            mode="compose",
            systems=(system,),
            bindings=bindings,
            budget=cast(Budget, object()),
        )
    malformed = read_value_set(_encoded(expansion=_expansion([{"code": "A"}], 1)))
    adopted = compile_value_set(
        malformed, mode="expansion", systems=(system,), bindings=bindings
    )
    assert any(issue.kind == "input_error" for issue in adopted.report)
