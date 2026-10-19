"""Original number tokens qualify explicit policies without silently changing data."""

import importlib.util
from decimal import getcontext
from typing import Any, cast

import pytest
from rdflib import Literal
from rdflib.namespace import XSD

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.jsonld.numbers import (
    NumericProjection,
    decode_numeric_json,
    project_numeric_json,
)
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.limits import Budget, Limits


@pytest.mark.skipif(
    importlib.util.find_spec("pyld") is None, reason="actual PyLD route"
)
def test_exact_source_catches_decoder_and_real_rdf_route_losses() -> None:
    """Exact decoding alone cannot qualify the maintained large-integer RDF route."""
    for token in ("1", "1.0", "0.5", "9007199254740993"):
        result = decode_numeric_json('{"urn:n":' + token + "}", policy="source-exact")
        assert result.report.verified, result.report.to_dict()
        assert result.observations[0].token == token
        assert result.observations[0].decoded_exact is True
        assert result.observations[0].rdf_exact is True
    for token in ("1.0000000000000001", "1e-400", "0.1"):
        raw = '{"urn:n":' + token + "}"
        ordinary = decode_numeric_json(raw)
        assert ordinary.report.verified
        assert ordinary.observations[0].decoded_exact is False
        exact = decode_numeric_json(raw, policy="source-exact")
        assert not exact.report.verified and exact.document_json is None
        assert any(i.code == "context.number_not_exact" for i in exact.report.issues)
    large = decode_numeric_json(
        '{"urn:n":1000000000000000000001}', policy="source-exact"
    )
    assert large.observations[0].decoded_exact is True
    assert large.observations[0].rdf_exact is False
    assert any(i.code == "context.number_rdf_not_exact" for i in large.report.issues)


def test_explicit_decimal_projection_retains_actual_exact_lexical_values() -> None:
    """Explicit datatype intent turns a raw number into a typed lexical value."""
    for token, lexical in (
        ("1.0000000000000001", "1.0000000000000001"),
        ("1e-400", "0." + "0" * 399 + "1"),
        ("1.25e2", "125"),
    ):
        projected = project_numeric_json(
            '{"@id":"urn:r","urn:n":' + token + "}",
            (NumericProjection(("urn:n",), str(XSD.decimal)),),
        )
        assert projected.report.verified, projected.report.to_dict()
        document = cast(dict[str, Any], projected.document)
        assert document["urn:n"] == {"@value": lexical, "@type": str(XSD.decimal)}
        document["urn:n"]["@value"] = "mutated"
        assert cast(dict[str, Any], projected.document)["urn:n"]["@value"] == lexical
        if importlib.util.find_spec("pyld") is not None:
            rdf = prepare_jsonld(projected.document)
            assert rdf.source is not None and rdf.report.verified
            actual = next(rdf.source.graph().objects())
            assert isinstance(actual, Literal) and actual.datatype == XSD.decimal
            assert str(actual) == lexical
    invalid = project_numeric_json(
        '{"n":1.5}', (NumericProjection(("n",), str(XSD.integer)),)
    )
    assert not invalid.report.verified and invalid.document_json is None
    integral = project_numeric_json(
        '{"n":1.00}', (NumericProjection(("n",), str(XSD.integer)),)
    )
    assert integral.report.verified
    assert cast(dict[str, Any], integral.document)["n"]["@value"] == "1"


def test_portable_policy_and_original_nested_paths_share_strict_json_decoding() -> None:
    """Real token paths distinguish input provenance from already decoded values."""
    result = decode_numeric_json('{"outer":[{"a/b":9007199254740993,"t~x":0.5}]}')
    assert result.report.verified
    assert [item.path for item in result.observations] == [
        ("outer", 0, "a/b"),
        ("outer", 0, "t~x"),
    ]
    for raw in ('{"n":9007199254740993}', '{"n":0.5}'):
        portable = decode_numeric_json(raw, policy="portable-integer")
        assert not portable.report.verified
        assert any(
            i.code == "context.number_not_portable" for i in portable.report.issues
        )
    assert decode_numeric_json(
        '{"n":9007199254740991}', policy="portable-integer"
    ).report.verified
    parsed: Any = 1.0
    unproven = decode_numeric_json(parsed, policy="source-exact")
    assert not unproven.report.verified and not unproven.observations
    duplicate = '{"outer":[{"a/b":1,"a/b":2}]}'
    with pytest.raises(InputError) as original:
        decode_json(duplicate)
    qualified = decode_numeric_json(duplicate)
    assert qualified.report.outcome == "input_error"
    assert "/outer/0/a~1b" in str(original.value)
    assert "/outer/0/a~1b" in qualified.report.issues[-1].message
    assert qualified.report.issues[-1].location == "/outer/0/a~1b"
    assert decode_json('{"n":1.0,"nested":[true,null]}') == {
        "n": 1.0,
        "nested": [True, None],
    }


def test_projection_does_not_reinterpret_keyword_or_non_numeric_payloads() -> None:
    """Value/context/JSON payloads cannot receive guessed wrappers."""
    for raw, path in (
        ('{"n":{"@value":1}}', ("n", "@value")),
        ('{"@context":{"value":1}}', ("@context", "value")),
        ('{"n":{"@value":{"v":1},"@type":"@json"}}', ("n", "@value", "v")),
    ):
        result = project_numeric_json(raw, (NumericProjection(path, str(XSD.decimal)),))
        assert result.report.outcome == "unverified" and result.document_json is None
    for raw, context_path in (
        ('{"@context":{"v":"@value","n":"urn:n"},"n":{"v":1}}', ("n", "v")),
        ('{"@context":{"n":{"@id":"urn:n","@type":"@json"}},"n":{"v":1}}', ("n", "v")),
        ('{"n":1,"@context":null}', ("n",)),
    ):
        contextual = project_numeric_json(
            raw, (NumericProjection(context_path, str(XSD.decimal)),)
        )
        assert contextual.report.outcome == "unverified"
        assert (
            contextual.document_json is None and contextual.observations[0].token == "1"
        )
    retained = project_numeric_json(
        '{"bad":0.1,"n":1,"@context":null}',
        (NumericProjection(("n",), str(XSD.decimal)),),
        policy="source-exact",
    )
    assert retained.document_json is None
    assert any(i.code == "context.number_not_exact" for i in retained.report.issues)
    assert any(i.code == "context.number_unverified" for i in retained.report.issues)
    for raw in ('{"n":"1"}', '{"other":1}'):
        result = project_numeric_json(
            raw, (NumericProjection(("n",), str(XSD.decimal)),)
        )
        assert not result.report.verified
        assert any(
            i.code == "context.number_projection_missing" for i in result.report.issues
        )
    unknown = project_numeric_json(
        '{"n":1}', (NumericProjection(("n",), "urn:custom"),)
    )
    assert unknown.report.outcome == "unverified"


def test_numeric_bounds_context_isolation_and_core_processor_coverage() -> None:
    """Bounds stop expansion and preserve earlier failures without global mutations."""
    context = getcontext()
    before = (
        context.prec,
        context.rounding,
        context.flags.copy(),
        context.traps.copy(),
    )
    projected = project_numeric_json(
        '{"n":0e-1000000000}',
        (NumericProjection(("n",), str(XSD.integer)),),
        limits=Limits(max_bytes=1000),
    )
    assert (
        projected.report.verified
        and cast(dict[str, Any], projected.document)["n"]["@value"] == "0"
    )
    huge = project_numeric_json(
        '{"n":1e-1000000000}',
        (NumericProjection(("n",), str(XSD.decimal)),),
        limits=Limits(max_bytes=1000),
    )
    assert huge.report.outcome == "exhausted" and huge.document_json is None
    assert huge.observations[0].token == "1e-1000000000"
    unsupported = decode_numeric_json('{"n":1e9999999999999999999999999}')
    assert unsupported.report.outcome == "unverified" and unsupported.observations
    escaped = decode_numeric_json(
        '{"' + "/" * 90 + '":1}', limits=Limits(max_bytes=128)
    )
    assert escaped.report.outcome == "exhausted"
    stopped = decode_numeric_json(
        '{"a":1.0000000000000001,"b":1e-400}',
        policy="source-exact",
        budget=Budget(Limits(max_issues=1)),
    )
    assert stopped.report.outcome == "exhausted"
    assert any(i.code == "context.number_not_exact" for i in stopped.report.issues)
    assert (
        context.prec,
        context.rounding,
        dict(context.flags),
        dict(context.traps),
    ) == before
    if importlib.util.find_spec("pyld") is None:
        missing = decode_numeric_json('{"n":1}', policy="source-exact")
        assert missing.report.outcome == "unverified" and missing.document_json is None
    with pytest.raises(InputError, match="conflict"):
        decode_numeric_json("1", limits=Limits(max_bytes=100), budget=Budget())
