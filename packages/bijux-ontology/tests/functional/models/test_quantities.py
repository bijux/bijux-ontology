"""Explicit native unit plans check real records without changing their originals."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Context, Decimal, Inexact, Rounded, getcontext, localcontext
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.quantities import (
    PreparedQuantities,
    QuantityContract,
    UnitBinding,
    prepare_quantities,
)
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("urn:quantity:")


def _bindings() -> tuple[UnitBinding, ...]:
    """Bind ordinary codes and actual IRI text independently of native names."""
    return (
        UnitBinding("mm", "millimeter"),
        UnitBinding(str(EX.m), "meter"),
        UnitBinding("s", "second"),
        UnitBinding("min", "minute"),
        UnitBinding("h", "hour"),
        UnitBinding("C", "degree_Celsius"),
        UnitBinding("K", "kelvin"),
    )


def _contract() -> QuantityContract:
    """Select a nested actual record and a reference-unit interval."""
    return QuantityContract(
        ("measurement", "value"),
        ("measurement", "unit"),
        str(EX.m),
        minimum=Decimal("0.4"),
        maximum=Decimal("0.6"),
    )


def _record(value: object = "500", unit: str = "mm") -> dict[str, object]:
    """Return ordinary producer output that does not depend on the checker."""
    return {"id": "sample", "measurement": {"value": value, "unit": unit}}


def test_scaled_affine_exact_records_and_real_native_consumer() -> None:
    """Scaled and affine quantities pass actual bounds and a native field consumer."""
    pytest.importorskip("pint")
    prepared = prepare_quantities(_bindings())
    assert prepared.report.verified
    with pytest.raises(InputError, match="prepare_quantities"):
        PreparedQuantities(_bindings(), None, {}, prepared.report)
    source = _record()
    checked = prepared.check(source, _contract(), convert=True)
    assert checked.report.verified and checked.reference_value == Decimal("0.500")
    assert checked.original_value == "500" and checked.original_unit == "mm"
    assert source == _record() and checked.source == source
    output = cast(dict[str, object], checked.converted)
    measurement = cast(dict[str, object], output["measurement"])
    assert measurement == {"value": "0.500", "unit": str(EX.m)}
    graph = (
        Graph()
        .add(
            (
                EX.sample,
                EX.value,
                Literal(
                    measurement["value"],
                    datatype=XSD.decimal,
                    normalize=False,
                ),
            )
        )
        .add((EX.sample, EX.unit, EX.m))
    )
    model = ModelDeclaration(
        "Measurement",
        (),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("literal", datatype=str(XSD.decimal)),),
            ),
            FieldDeclaration("unit", str(EX.unit), (ValueDeclaration("iri"),)),
        ),
    )
    native = check_rdf_emission(
        prepare_source(RdfSource(graph)),
        (model,),
        (RdfModelUsage("Measurement", EX.sample),),
    )
    assert native.verified
    measurement["value"] = "tampered"
    assert cast(dict[str, object], checked.converted)["measurement"] != measurement
    source.clear()
    assert checked.source == _record()
    assert prepared.check(_record("0.5", str(EX.m)), _contract()).report.verified
    assert not prepared.check(_record("900"), _contract()).report.verified
    wrong_dimension = prepared.check(_record("500", "s"), _contract())
    assert wrong_dimension.report.outcome == "disagreement"
    assert "quantity.dimension_mismatch" in {
        i.code for i in wrong_dimension.report.issues
    }
    temperature = replace(
        _contract(), reference_unit="K", minimum=Decimal("298"), maximum=Decimal("299")
    )
    affine = prepared.check(_record("25", "C"), temperature, convert=True)
    assert affine.report.verified and affine.reference_value == Decimal("298.15")


def test_prepared_exactness_cache_and_hostile_decimal_context() -> None:
    """Repeated and concurrent native calls cannot upgrade an inexact factor."""
    pytest.importorskip("pint")
    prepared = prepare_quantities(_bindings())
    minute = replace(_contract(), reference_unit="h", minimum=None, maximum=None)
    with localcontext(Context(prec=2, Emin=-2, Emax=2, clamp=1)) as ambient:
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        before = ambient.copy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: prepared.check(_record("1", "min"), minute),
                    range(4),
                )
            )
        assert all(result.report.outcome == "unverified" for result in results)
        for _ in range(2):
            assert (
                prepared.check(_record("1", "min"), minute).report.outcome
                == "unverified"
            )
        exact = prepared.check(_record("1", "h"), replace(minute, reference_unit="min"))
        assert exact.report.verified and exact.reference_value == 60
        long = "123456789012345678901234567890.123456789"
        unconstrained = replace(_contract(), minimum=None, maximum=None)
        scaled = prepared.check(_record(long), unconstrained, convert=True)
        assert scaled.report.verified
        assert str(scaled.reference_value) == "123456789012345678901234567.890123456789"
        assert str(ambient) == str(before) and ambient.flags == before.flags
    assert getcontext().prec != 2


def test_unknown_units_numeric_profile_and_missing_fields() -> None:
    """Unbound, offset compound, logarithmic and invalid samples never certify."""
    pytest.importorskip("pint")
    prepared = prepare_quantities(_bindings())
    for unit in ("unbound", "meter", "urn:unknown:unit"):
        result = prepared.check(_record("500", unit), _contract(), convert=True)
        assert result.report.outcome == "unverified" and result.converted is None
    for name in ("degree_Fahrenheit", "decibel", "degree_Celsius/second"):
        unsupported = prepare_quantities((UnitBinding("u", name),))
        assert unsupported.report.outcome == "unverified"
        result = unsupported.check(
            _record("1", "u"), replace(_contract(), reference_unit="u")
        )
        assert result.report.outcome == "unverified"
    unsupported_values: tuple[object, ...] = (0.5, True, None, [])
    for value in unsupported_values:
        assert (
            prepared.check(_record(value), _contract()).report.outcome == "unverified"
        )
    invalid = prepared.check(_record("not-decimal"), _contract())
    assert invalid.report.outcome == "disagreement"
    absent = prepared.check({"measurement": {"unit": "mm"}}, _contract())
    assert absent.report.outcome == "disagreement"
    duplicate = prepare_quantities(
        (UnitBinding("m", "meter"), UnitBinding("m", "second"))
    )
    assert duplicate.report.outcome == "input_error"
    overlap = prepared.check(
        _record(), replace(_contract(), unit_path=("measurement",))
    )
    assert overlap.report.outcome == "input_error"


def test_cumulative_bounds_metadata_and_paid_finding_prefix() -> None:
    """Actual paid interval failures survive later work and issue exhaustion."""
    pytest.importorskip("pint")
    prepared = prepare_quantities(_bindings())
    measured = Budget()
    assert (
        prepared.check(_record("900"), _contract(), budget=measured).report.outcome
        == "disagreement"
    )
    used = measured.limits.max_work - measured.remaining("work")
    operation = Budget(Limits(max_work=used))
    first = prepared.check(_record("900"), _contract(), budget=operation)
    assert first.report.outcome == "disagreement"
    later = prepared.check(_record("500"), _contract(), budget=operation)
    assert later.report.outcome == "exhausted"
    huge = replace(_contract(), reference_unit="x" * 100000)
    assert (
        prepared.check(_record(), huge, limits=Limits(max_bytes=1000)).report.outcome
        == "exhausted"
    )
    enormous = replace(_contract(), minimum=Decimal("9" * 100000), maximum=None)
    assert (
        prepared.check(
            _record(), enormous, limits=Limits(max_bytes=50000)
        ).report.outcome
        == "exhausted"
    )
    for cap in (1, 50, 100, 200, 400):
        bounded = prepared.check(_record(), _contract(), limits=Limits(max_states=cap))
        assert bounded.report.outcome in ("exhausted", "agreement")
    assert (
        prepare_quantities(_bindings(), limits=Limits(max_work=1)).report.outcome
        == "exhausted"
    )
    cyclic: dict[str, object] = {}
    cyclic["nested"] = cyclic
    for depth in (128, 10000):
        assert (
            prepared.check(
                cyclic, _contract(), limits=Limits(max_depth=depth)
            ).report.outcome
            == "exhausted"
        )
    assert prepared.check(_record(), _contract()).report.verified
