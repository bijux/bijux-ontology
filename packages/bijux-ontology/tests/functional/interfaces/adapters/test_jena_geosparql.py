"""Actual maintained local geometry, axis, units and unavailable-work controls."""

import os
from pathlib import Path
from threading import Event

import pytest
from rdflib import Literal, URIRef

from bijux_ontology.interfaces.adapters.jena import JenaBackend, prepare_jena_backend
from bijux_ontology.interfaces.adapters.jena.geosparql import (
    GeometryValue,
    SpatialDistance,
    SpatialRelation,
    check_geometries,
)
from bijux_ontology.limits import Budget, Limits

CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
EPSG4326 = "http://www.opengis.net/def/crs/EPSG/0/4326"
WKT = URIRef("http://www.opengis.net/ont/geosparql#wktLiteral")
GML = URIRef("http://www.opengis.net/ont/geosparql#gmlLiteral")
DEGREE = "http://www.opengis.net/def/uom/OGC/1.0/degree"


def value(key: str, lexical: str, datatype: URIRef = WKT) -> GeometryValue:
    """Retain exact caller-supplied geometry text and an established location."""
    return GeometryValue(
        key, Literal(lexical, datatype=datatype, normalize=False), "/" + key
    )


@pytest.fixture
def backend(tmp_path: Path) -> JenaBackend:
    """Use only an explicitly supplied sealed local native runtime."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_JENA_JAVA",
            "BIJUX_JENA_BUNDLE",
            "BIJUX_JENA_MANIFEST_SHA256",
        )
    )
    if not (java and bundle and pin):
        pytest.skip("An explicit pinned optional Jena distribution is required")
    return prepare_jena_backend(
        Path(java), Path(bundle), manifest_sha256=pin, artifacts=tmp_path / "native"
    )


def test_native_topology_axes_literals_and_distance(backend: JenaBackend) -> None:
    """Actual CRS axis conversion and topology do not rewrite RDF source identity."""
    values = (
        value("polygon", "POLYGON((10 40,20 40,20 50,10 50,10 40))"),
        value("inside", "POINT(15 45)"),
        value("outside", "POINT(5 45)"),
        value("latlon", f"<{EPSG4326}> POINT(45 15)"),
        value("wrong_axis", f"<{EPSG4326}> POINT(15 45)"),
        value("alias", "POINT(15.0 45.0)"),
        value("near", "POINT(16 45)"),
    )
    original = tuple(str(item.literal) for item in values)
    checked = check_geometries(
        backend,
        values,
        (
            SpatialRelation("inside", "polygon", "within"),
            SpatialRelation("outside", "polygon", "within", False),
            SpatialRelation("latlon", "polygon", "within"),
            SpatialRelation("wrong_axis", "polygon", "within", False),
            SpatialRelation("inside", "alias", "topological_equals"),
        ),
        (SpatialDistance("inside", "near", 1.0, DEGREE),),
    )
    assert checked.report.verified and checked.report.checked == 13
    assert checked.geometries[3].axis_order == "latitude_longitude"
    assert checked.geometries[1].crs == CRS84
    assert checked.relations[-1].value is True
    assert checked.relations[-1].rdf_terms_equal is False
    assert checked.distances[0].value == 1.0
    assert tuple(str(item.literal) for item in checked.geometries) == original
    independent = checked.geometries[0].literal
    independent._datatype = URIRef("urn:changed")
    assert checked.geometries[0].literal.datatype == WKT
    negative = check_geometries(
        backend,
        values[:3],
        (SpatialRelation("outside", "polygon", "within", location="/outside"),),
        (SpatialDistance("inside", "outside", 1.0, DEGREE),),
    )
    assert negative.report.outcome == "disagreement" and negative.report.complete
    assert negative.relations[0].value is False and negative.distances[0].value == 10.0
    assert any(issue.location == "/outside" for issue in negative.report.issues)


def test_lexical_invalidity_value_invalidity_and_unknown_dependencies(
    backend: JenaBackend,
) -> None:
    """A proved malformed value stays negative while dependent topology is unknown."""
    malformed = check_geometries(
        backend,
        (value("bad", "POINT(not-a-number)"), value("ok", "POINT(0 0)")),
        (SpatialRelation("bad", "ok", "within"),),
    )
    assert malformed.geometries[0].lexical_valid is False
    assert malformed.geometries[0].report.outcome == "disagreement"
    assert malformed.relations[0].value is None and not malformed.report.complete
    assert {issue.kind for issue in malformed.report.issues} == {
        "finding",
        "unverified",
    }
    invalid = check_geometries(
        backend, (value("bowtie", "POLYGON((0 0,2 2,0 2,2 0,0 0))"),)
    )
    assert invalid.geometries[0].lexical_valid is True
    assert invalid.geometries[0].geometry_valid is False
    assert invalid.report.complete and invalid.report.outcome == "disagreement"
    empty = check_geometries(backend, (value("empty", ""),))
    assert empty.report.verified and empty.geometries[0].empty is True
    for source in (
        value("unknown", "<urn:unknown:crs> POINT(1 2)"),
        value("projected", "<http://www.opengis.net/def/crs/EPSG/0/3857> POINT(1 2)"),
        value("other", "{}", URIRef("urn:unsupported:serialization")),
        value("dimension", "POINT Z (1 2 3)"),
    ):
        result = check_geometries(backend, (source,))
        assert not result.report.verified and not result.report.complete
        assert result.geometries[0].report.outcome == "unverified"


def test_native_gml_point_and_secure_profile_guards(backend: JenaBackend) -> None:
    """Actual GML Point/pos follows declared axes and rejects unhandled structure."""
    xml = (
        '<gml:Point xmlns:gml="http://www.opengis.net/gml/3.2" '
        f'srsName="{EPSG4326}"><gml:pos>45 15</gml:pos></gml:Point>'
    )
    result = check_geometries(
        backend,
        (value("gml", xml, GML), value("wkt", "POINT(15 45)")),
        (SpatialRelation("gml", "wkt", "topological_equals"),),
    )
    assert result.report.verified and result.relations[0].value is True
    assert result.relations[0].rdf_terms_equal is False
    assert str(result.geometries[0].literal) == xml
    for bad in (
        "<!DOCTYPE Point SYSTEM 'http://127.0.0.1:9/no'>" + xml,
        xml.replace("/gml/3.2", "/gml"),
        xml.replace("gml:pos", "gml:coordinates"),
        xml.replace("45 15", "45 15 99"),
        xml.replace("</gml:Point>", "<gml:pos>45 15</gml:pos></gml:Point>"),
    ):
        guarded = check_geometries(backend, (value("guard", bad, GML),))
        assert not guarded.report.verified and not guarded.report.complete
    unknown_unit = check_geometries(
        backend,
        (value("a", "POINT(0 0)"), value("b", "POINT(1 0)")),
        distances=(
            SpatialDistance(
                "a", "b", 1000.0, "http://www.opengis.net/def/uom/OGC/1.0/metre"
            ),
        ),
    )
    assert unknown_unit.distances[0].value is None
    assert unknown_unit.report.outcome == "unverified"


def test_inventory_budget_and_cancel_cannot_publish_success(
    backend: JenaBackend,
) -> None:
    """Ambiguous references, bounded interruption and missing inputs cannot pass."""
    for values, relations in (
        ((), ()),
        ((value("same", "POINT(0 0)"), value("same", "POINT(1 1)")), ()),
        ((value("a", "POINT(0 0)"),), (SpatialRelation("a", "missing", "within"),)),
    ):
        assert (
            check_geometries(backend, values, relations).report.outcome == "input_error"
        )
    for limits in (
        Limits(max_total_bytes=4096),
        Limits(max_work=100),
        Limits(max_states=1),
    ):
        budget = Budget(limits)
        result = check_geometries(backend, (value("a", "POINT(0 0)"),), budget=budget)
        assert result.report.outcome == "exhausted" and not result.report.verified
        assert all(
            count
            <= getattr(limits, "max_total_bytes" if kind == "bytes" else "max_" + kind)
            for kind, count in budget._counts.items()
        )
    cancel = Event()
    cancel.set()
    budget = Budget()
    assert (
        check_geometries(
            backend, (value("a", "POINT(0 0)"),), cancel=cancel, budget=budget
        ).report.outcome
        == "exhausted"
    )
    assert budget._counts == {}
    assert check_geometries(backend, (value("a", "POINT(0 0)"),)).report.verified
