"""Real value comparisons preserve RDF terms and explicit regime boundaries."""

from dataclasses import FrozenInstanceError
from importlib.metadata import PackageNotFoundError
from typing import Final

import pytest
from rdflib import XSD, Literal

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.datatype_evaluators import DatatypeEvaluator, DatatypeRegistry
from bijux_ontology.rdf.terms import literal_term, term_key
from bijux_ontology.rdf.value_comparison import compare_literal_values

SPARQL: Final = "sparql-1.1"
OWL: Final = "owl2-datatype-map"


def _literal(text: str, datatype: str = "integer") -> Literal:
    """Preserve the exact lexical input rather than a Python value conversion."""
    return literal_term(text, datatype=str(XSD) + datatype)


def test_exact_integer_decimal_and_boolean_values_do_not_replace_rdf_terms() -> None:
    left, right = _literal("+0012"), _literal("12")
    result = compare_literal_values(left, right, regime=SPARQL)
    assert result.relation == "equal" and result.report.verified
    assert result.left is left and result.right is right
    assert term_key(left) != term_key(right) and str(left) == "+0012"
    with pytest.raises(FrozenInstanceError):
        result.relation = "different"  # type: ignore[misc]
    for a, b, kind in (
        ("9007199254740992", "9007199254740993", "integer"),
        ("0.1000000000000000000000000001", "0.1000000000000000000000000002", "decimal"),
        ("true", "false", "boolean"),
    ):
        compared = compare_literal_values(
            _literal(a, kind), _literal(b, kind), regime=SPARQL
        )
        assert compared.relation == "different" and compared.report.verified
    assert (
        compare_literal_values(
            _literal("01"), _literal("1.00", "decimal"), regime=OWL
        ).relation
        == "equal"
    )
    assert (
        compare_literal_values(
            _literal("true", "boolean"), _literal("1", "boolean"), regime=SPARQL
        ).relation
        == "equal"
    )
    assert (
        compare_literal_values(
            _literal("true", "boolean"), _literal("1"), regime=SPARQL
        ).relation
        == "indeterminate"
    )
    assert (
        compare_literal_values(
            _literal("true", "boolean"), _literal("1"), regime=OWL
        ).relation
        == "different"
    )


def test_numeric_promotion_and_float32_boundaries_use_actual_maintained_backend() -> (
    None
):
    for left, right in (
        (_literal("1"), _literal("1.0E0", "double")),
        (_literal("1", "float"), _literal("1", "double")),
        (_literal("9007199254740993"), _literal("9007199254740992E0", "double")),
    ):
        assert compare_literal_values(left, right, regime=SPARQL).relation == "equal"
        assert compare_literal_values(left, right, regime=OWL).relation == "different"
    for a, b in (
        ("1.00000006", "1"),
        ("1.401298464324817e-45", "0"),
        ("1.0000000596046447753906250000000000000000000001", "1"),
    ):
        result = compare_literal_values(
            _literal(a, "float"), _literal(b, "float"), regime=SPARQL
        )
        assert result.relation == "different" and result.report.verified
    # Backend range failures are unbound; exact integer pairs bypass that limit.
    large = _literal("340282366920938463463374607431768211456")
    limited = compare_literal_values(large, _literal("1", "double"), regime=SPARQL)
    assert limited.relation == "indeterminate" and not limited.report.verified


def test_signed_zero_identity_and_nan_do_not_become_python_equality() -> None:
    for datatype in ("float", "double"):
        negative, positive = _literal("-0", datatype), _literal("0", datatype)
        assert (
            compare_literal_values(negative, positive, regime=SPARQL).relation
            == "equal"
        )
        assert (
            compare_literal_values(negative, positive, regime=OWL).relation
            == "different"
        )
        assert (
            compare_literal_values(
                _literal("-1e-5000", datatype), negative, regime=OWL
            ).relation
            == "equal"
        )
        for regime in (SPARQL, OWL):
            nan = compare_literal_values(
                _literal("NaN", datatype), _literal("NaN", datatype), regime=regime
            )
            assert (
                nan.relation == "indeterminate" and nan.report.outcome == "unverified"
            )


def test_calendar_instants_offsets_and_qualified_precision_are_separate() -> None:
    pytest.importorskip("elementpath")
    for a, b in (
        ("2000-01-01T00:00:00Z", "1999-12-31T19:00:00-05:00"),
        ("2020-01-01T00:00:00Z", "2020-01-01T01:00:00+01:00"),
    ):
        left, right = _literal(a, "dateTime"), _literal(b, "dateTime")
        assert compare_literal_values(left, right, regime=SPARQL).relation == "equal"
        assert compare_literal_values(left, right, regime=OWL).relation == "different"
    midnight = _literal("2020-01-01T24:00:00Z", "dateTime")
    next_day = _literal("2020-01-02T00:00:00Z", "dateTimeStamp")
    assert compare_literal_values(midnight, next_day, regime=OWL).relation == "equal"
    assert (
        compare_literal_values(midnight, next_day, regime=SPARQL).relation
        == "indeterminate"
    )
    for text in (
        "2020-01-01T00:00:00",
        "2020-01-01T00:00:00.0000001Z",
        "10000-01-01T00:00:00Z",
    ):
        for regime in (SPARQL, OWL):
            result = compare_literal_values(
                _literal(text, "dateTime"),
                _literal("2020-01-01T00:00:00Z", "dateTime"),
                regime=regime,
            )
            assert (
                result.relation == "indeterminate"
                and result.report.outcome == "unverified"
            )
    assert (
        compare_literal_values(
            _literal("2020-01-01T00:00:00.000001Z", "dateTime"),
            _literal("2020-01-01T00:00:00.000002Z", "dateTime"),
            regime=SPARQL,
        ).relation
        == "different"
    )


def test_custom_conversion_is_not_equality_and_invalid_samples_stay_findings() -> None:
    custom = literal_term("01", datatype="urn:custom-number")
    calls: list[Literal] = []

    def conversion(value: Literal) -> int:
        """Expose that a value converter cannot supply missing equality semantics."""
        calls.append(value)
        return 1

    registry = DatatypeRegistry(
        (
            DatatypeEvaluator(
                "urn:custom-number",
                validators=(lambda value: True,),
                converter=conversion,
            ),
        )
    )
    for regime in (SPARQL, OWL):
        result = compare_literal_values(
            custom, custom, regime=regime, datatype_registry=registry
        )
        assert result.relation == "indeterminate" and not result.report.verified
    assert calls == []
    assert (
        compare_literal_values(custom, custom, regime=SPARQL).relation
        == "indeterminate"
    )
    mutated = literal_term("01", datatype="urn:mutable-number")

    def mutate_metadata(value: Literal) -> bool:
        """A caller callback cannot provide an unsupported comparison capability."""
        value._datatype = XSD.integer
        return True

    mutable_registry = DatatypeRegistry(
        (DatatypeEvaluator("urn:mutable-number", validators=(mutate_metadata,)),)
    )
    assert (
        compare_literal_values(
            mutated, _literal("1"), regime=SPARQL, datatype_registry=mutable_registry
        ).relation
        == "indeterminate"
    )
    invalid = _literal(" 1 ")
    result = compare_literal_values(invalid, _literal("1"), regime=SPARQL)
    assert (
        result.relation == "indeterminate" and result.report.outcome == "disagreement"
    )
    assert "literal.lexical_invalid" in {issue.code for issue in result.report.issues}
    assert str(invalid) == " 1 "


def test_owned_bounds_and_input_contracts_cannot_yield_a_false_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = _literal("1"), _literal("2")
    operation = Budget(Limits(max_work=10))
    operation.consume("work", 9)
    result = compare_literal_values(left, right, regime=SPARQL, budget=operation)
    assert result.relation == "indeterminate" and result.report.outcome == "exhausted"
    assert result.left is left and result.right is right
    partial = compare_literal_values(
        _literal("bad"),
        _literal("x" * 1000, "string"),
        regime=SPARQL,
        budget=Budget(Limits(max_work=100)),
    )
    assert partial.report.outcome == "exhausted"
    assert "literal.lexical_invalid" in {issue.code for issue in partial.report.issues}
    answer_bound = compare_literal_values(
        _literal("1"),
        _literal("1", "double"),
        regime=SPARQL,
        budget=Budget(Limits(max_work=216)),
    )
    assert answer_bound.report.outcome == "exhausted"
    huge = _literal("x" * 100_000, "string")
    bounded = compare_literal_values(
        huge, right, regime=SPARQL, budget=Budget(Limits(max_bytes=100))
    )
    assert bounded.report.outcome == "exhausted" and bounded.left is huge
    assert sum(len(issue.message) for issue in bounded.report.issues) < 1000
    for kwargs in (
        {"regime": "guess"},
        {"regime": SPARQL, "budget": 1},
        {"regime": SPARQL, "datatype_registry": object()},
    ):
        with pytest.raises(InputError):
            compare_literal_values(left, right, **kwargs)
    with pytest.raises(InputError):
        compare_literal_values("1", right, regime=SPARQL)  # type: ignore[arg-type]

    def unavailable_backend(name: str) -> str:
        """Exercise actual public handling of an unavailable optional dependency."""
        raise PackageNotFoundError(name)

    monkeypatch.setattr(
        "bijux_ontology.rdf.value_comparison.comparing.version", unavailable_backend
    )
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    calendar = _literal("2020-01-01T00:00:00Z", "dateTime")
    unavailable = compare_literal_values(
        calendar, calendar, regime=SPARQL, budget=spent
    )
    assert unavailable.report.outcome == "exhausted"
