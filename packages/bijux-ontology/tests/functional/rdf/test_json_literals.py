"""Opaque RDF JSON literal contracts preserve source terms and exact values."""

import importlib.util
from decimal import Decimal, getcontext, localcontext
from typing import Any, cast

import pytest
from rdflib import Literal
from rdflib.namespace import RDF, XSD

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.schema import DIALECT
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.rdf.literal_values.json import (
    JsonLiteralContract,
    JsonRequirement,
    check_json_literal,
)
from bijux_ontology.rdf.terms import term_key


def _literal(text: str) -> Literal:
    """Keep exact lexical source bytes independent of parsed JSON identity."""
    return Literal(text, datatype=RDF.JSON, normalize=False)


def test_real_jsonld_literal_nested_contracts_have_no_invented_rdf_edges() -> None:
    pytest.importorskip("pyld")
    document = {
        "@id": "urn:record",
        "urn:payload": {
            "@type": "@json",
            "@value": {"scores": [1, 2], "metadata": {"active": True}},
        },
    }
    source = prepare_jsonld(document)
    assert source.report.verified and source.source is not None
    graph = source.source.graph()
    original = next(graph.objects())
    assert isinstance(original, Literal) and original.datatype == RDF.JSON
    contract = JsonLiteralContract(
        (
            JsonRequirement(("scores",), kind="array", min_items=2, max_items=2),
            JsonRequirement(("scores", 1), kind="integer", minimum=Decimal("2")),
            JsonRequirement(("metadata", "active"), expected_json="true"),
        )
    )
    result = check_json_literal(original, contract, location="record/payload")
    assert result.report.verified and result.original is original
    assert len(graph) == 1
    changed = check_json_literal(
        _literal('{"scores":[1,1],"metadata":{"active":true}}'),
        contract,
        location="record/payload",
    )
    assert changed.report.outcome == "disagreement"
    issue = changed.report.issues[0]
    assert issue.location == "record/payload#/scores/1"
    assert issue.details is not None and issue.details.path == ("scores", 1)
    assert len(graph) == 1


def test_strict_syntax_paths_and_rdf_identity_are_separate_from_value_equality() -> (
    None
):
    for text in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}', "[1,]", ""):
        assert check_json_literal(
            _literal(text), JsonLiteralContract()
        ).report.outcome == ("input_error")
    duplicate = check_json_literal(
        _literal('{"outer":[{"a/b":1,"a\\u002fb":2}]}'), JsonLiteralContract()
    )
    assert duplicate.report.outcome == "input_error"
    assert duplicate.report.issues[0].location == "literal#/outer/0/a~1b"
    contract = JsonLiteralContract((JsonRequirement((), expected_json='{"x":1}'),))
    left, right = _literal('{"x":1}'), _literal('{ "x":1.0 }')
    assert term_key(left) != term_key(right)
    assert check_json_literal(left, contract).report.verified
    result = check_json_literal(right, contract)
    assert result.report.verified and result.original is right
    cast(dict[str, object], result.value)["x"] = "mutated"
    assert cast(dict[str, object], result.value)["x"] == Decimal("1.0")
    assert not check_json_literal(
        _literal("[2,1]"),
        JsonLiteralContract((JsonRequirement((), expected_json="[1,2]"),)),
    ).report.verified
    for path, where in (
        ((), "literal#"),
        (("",), "literal#/"),
        (("a/b~",), "literal#/a~1b~0"),
    ):
        mismatch = check_json_literal(
            _literal('{"":0,"a/b~":0}'),
            JsonLiteralContract((JsonRequirement(path, kind="string"),)),
        )
        assert mismatch.report.issues[0].location == where
    wrong = check_json_literal(Literal("{}", datatype=XSD.string), contract)
    assert wrong.report.outcome == "disagreement" and not wrong.decoded


def test_exact_numbers_boolean_distinction_and_decimal_context_independence() -> None:
    original = getcontext().copy()
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        for text in ("0.1", "1000000000000000000001", "1e-400", "1.00", "-0"):
            value = Decimal(text)
            result = check_json_literal(
                _literal(text),
                JsonLiteralContract(
                    (JsonRequirement((), kind="number", minimum=value, maximum=value),)
                ),
            )
            assert result.report.verified, result.report.to_dict()
            assert result.value == value
        assert check_json_literal(
            _literal("1.0"), JsonLiteralContract((JsonRequirement((), kind="integer"),))
        ).report.verified
        assert (
            check_json_literal(
                _literal("1.001"),
                JsonLiteralContract((JsonRequirement((), kind="integer"),)),
            ).report.outcome
            == "disagreement"
        )
        assert (
            check_json_literal(
                _literal("true"),
                JsonLiteralContract((JsonRequirement((), expected_json="1"),)),
            ).report.outcome
            == "disagreement"
        )
        assert (
            check_json_literal(
                _literal("true"),
                JsonLiteralContract((JsonRequirement((), kind="number"),)),
            ).report.outcome
            == "disagreement"
        )
    assert str(getcontext()) == str(original)
    unavailable = check_json_literal(
        _literal("1e999999999999999999999999999999999999999"), JsonLiteralContract()
    )
    assert unavailable.report.outcome == "unverified" and not unavailable.decoded


def test_maintained_schema_exact_boundaries_local_refs_and_finite_capabilities() -> (
    None
):
    schema: dict[str, object] = {
        "$schema": DIALECT,
        "$id": "urn:payload-schema",
        "type": "object",
        "$defs": {"amount": {"type": "number", "minimum": 0.1, "maximum": 0.1}},
        "properties": {
            "amount": {"$ref": "#/$defs/amount"},
            "count": {"type": "integer"},
        },
        "required": ["amount", "count"],
        "additionalProperties": False,
    }
    contract = JsonLiteralContract(schema=schema, dialect=DIALECT)
    result = check_json_literal(_literal('{"amount":0.1,"count":1.0}'), contract)
    if importlib.util.find_spec("jsonschema") is None:
        assert result.report.outcome == "unverified" and result.decoded
        assert "[jsonschema]" in result.report.issues[0].message
        return
    assert result.report.verified, result.report.to_dict()
    for text, pointer in (
        ('{"amount":0.099999999999999999999,"count":1}', "/amount"),
        ('{"amount":0.1,"count":1.000000000000000000001}', "/count"),
        ('{"amount":0.1,"count":true}', "/count"),
    ):
        failed = check_json_literal(_literal(text), contract)
        assert failed.report.outcome == "disagreement"
        assert failed.report.issues[0].location == "literal#" + pointer
    for extra in (
        {"multipleOf": 0.1},
        {"properties": {"count": {"$schema": DIALECT, "type": "integer"}}},
        {"$ref": "https://unavailable.invalid/schema"},
        {"$ref": "#"},
    ):
        requested = dict(schema)
        requested.update(extra)
        unsupported = check_json_literal(
            _literal('{"amount":0.1,"count":1}'),
            JsonLiteralContract(schema=requested, dialect=DIALECT),
        )
        assert unsupported.report.outcome == "unverified"
    assert (
        check_json_literal(
            _literal("1"), JsonLiteralContract(schema=schema, dialect="unknown")
        ).report.outcome
        == "unverified"
    )


def test_bounded_diagnostics_shared_counters_and_paid_requirement_prefix() -> None:
    source = _literal('{"wrong":false,"tail":[0,1,2,3,4]}')
    contract = JsonLiteralContract(
        (JsonRequirement(("wrong",), kind="number"),)
        + tuple(JsonRequirement(("tail", i), kind="integer") for i in range(5))
    )
    witnessed = 0
    for work in range(100, 2500, 25):
        result = check_json_literal(source, contract, limits=Limits(max_work=work))
        if result.report.outcome == "exhausted" and any(
            i.code == "literal.json_mismatch" for i in result.report.issues
        ):
            witnessed += 1
    assert witnessed > 0
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    result = check_json_literal(source, contract, budget=spent)
    assert result.report.outcome == "exhausted"
    for text, options in (
        ('"' + "😀" * 1000 + '"', Limits(max_bytes=1000)),
        ("[" * 50 + "0" + "]" * 50, Limits(max_depth=10)),
        ("[1,2,3]", Limits(max_states=2)),
    ):
        bounded = check_json_literal(
            _literal(text), JsonLiteralContract(), limits=options
        )
        assert bounded.report.outcome == "exhausted"
    supplied = check_json_literal(
        _literal("1"),
        JsonLiteralContract((JsonRequirement((), minimum=Decimal("1" * 2000)),)),
        limits=Limits(max_bytes=1000),
    )
    assert supplied.report.outcome == "exhausted" and not supplied.decoded
    aggregate = check_json_literal(
        _literal('{"' + "é" * 200 + '":false}'),
        JsonLiteralContract((JsonRequirement(("é" * 200,), kind="number"),)),
        location="é" * 400,
        limits=Limits(max_bytes=1000),
    )
    assert aggregate.report.outcome == "exhausted"
    assert all(len(i.location.encode("utf-8")) <= 1000 for i in aggregate.report.issues)
    malformed: Any = JsonRequirement((True,), kind="number")
    assert (
        check_json_literal(
            _literal("1"), JsonLiteralContract((malformed,))
        ).report.outcome
        == "input_error"
    )
    fresh = check_json_literal(_literal("null"), JsonLiteralContract())
    assert fresh.report.verified and fresh.decoded and fresh.value is None
