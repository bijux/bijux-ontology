"""Selected W3C datatype boundaries in exact literals and actual emitted RDF.

Valid samples retain NIST atomic fixtures from w3c/xsdtests/nistData and IBM
D3_4_26/v01 and D3_4_27/v01 duration examples. Invalid duration examples also
include IBM's si02 schema values; other negatives exercise XSD 1.1 productions.
"""

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.models.emission import ModelUsage, check_emission
from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.reports import Report

EX = Namespace("https://lexical.example/")


def _model(datatype: URIRef) -> ModelDeclaration:
    """Require the actual emitted datatype without relying on Python conversion."""
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("literal", datatype=str(datatype)),),
                emission="guaranteed",
            ),
        ),
    )


def _emitted(datatype: URIRef, lexical: str) -> Report:
    """Check owned native RDF while retaining both original and snapshot term keys."""
    term = Literal(lexical, datatype=datatype, normalize=False)
    identity = term_key(term)
    graph = (
        Graph().add((EX.record, RDF.type, EX.Record)).add((EX.record, EX.value, term))
    )
    source = prepare_source(graph)
    report = check_rdf_emission(
        source, [_model(datatype)], [RdfModelUsage("record", EX.record)]
    )
    assert term_key(term) == identity
    assert term_key(source.graph().value(EX.record, EX.value)) == identity
    return report


@pytest.mark.parametrize(
    "datatype,valid,invalid",
    [
        (XSD.duration, "P1970Y01M01DT00H00M01S", "PT"),
        (XSD.yearMonthDuration, "-P3Y348M", "P1Y1m"),
        (XSD.dayTimeDuration, "-PT46H59M3454.3S", "P1Y4D"),
        (XSD.hexBinary, "71", "0aF"),
        (XSD.base64Binary, "YQ==", "M0SyLMT="),
        (XSD.gYear, "1971", "01971"),
        (XSD.gYearMonth, "1970-02", "1970-13"),
        (XSD.gMonthDay, "--01-02", "--02-30"),
        (XSD.gMonth, "--02", "--13"),
        (XSD.gDay, "---02", "---32"),
        (XSD.Name, "h", "1name"),
        (XSD.NCName, "g", "p:name"),
        (XSD.NMTOKEN, "o", "two words"),
        (XSD.anyURI, "ftp://p.org", "\x00"),
    ],
)
def test_authoritative_boundaries_reach_actual_emitted_rdf(
    datatype: URIRef, valid: str, invalid: str
) -> None:
    """Each supported family checks actual valid and invalid emitted literals."""
    for lexical, accepted in ((valid, True), (invalid, False)):
        literal = Literal(lexical, datatype=datatype, normalize=False)
        original = term_key(literal)
        direct = check_literal(literal)
        native = _emitted(datatype, lexical)
        assert direct.verified is native.verified is accepted
        assert term_key(literal) == original
        if not accepted:
            assert {issue.code for issue in direct.issues} == {
                "literal.lexical_invalid"
            }
            assert "literal.lexical_invalid" in {issue.code for issue in native.issues}


@pytest.mark.parametrize(
    "datatype,valid,invalid",
    [
        (XSD.yearMonthDuration, "P0Y", "P0D"),
        (XSD.dayTimeDuration, "PT0S", "P0Y"),
        (XSD.duration, "PT0.00000001S", "P1D\n"),
        (XSD.hexBinary, "0aFF", " 0aFF "),
        (XSD.base64Binary, "Z g ==", "Zh=="),
        (XSD.gYear, "0000", "+0000"),
        (XSD.gMonthDay, "--02-29+14:00", "--02-29+14:01"),
        (XSD.Name, "p:name", "name\n"),
        (XSD.NMTOKEN, "1:token", ""),
    ],
)
def test_raw_lexical_grammar_cannot_be_repaired_by_normalization(
    datatype: URIRef, valid: str, invalid: str
) -> None:
    """Subtype zeroes, pad bits, whitespace and XSD 1.1 year zero remain distinct."""
    assert _emitted(datatype, valid).verified
    assert _emitted(datatype, invalid).outcome == "disagreement"


@pytest.mark.parametrize(
    "lexical", ["", "../relative", "%ZZ", "a b", "\t\r\n", "<tag>&data"]
)
def test_any_uri_checks_xml_character_space_without_claiming_iri_validity(
    lexical: str,
) -> None:
    """XSD 1.1 anyURI lexical validity neither resolves nor validates an RDF IRI."""
    assert _emitted(XSD.anyURI, lexical).verified


@pytest.mark.parametrize(
    "datatype",
    [XSD.Name, XSD.NCName, XSD.NMTOKEN, XSD.QName, XSD.NOTATION, XSD.IDREF, XSD.ENTITY],
)
def test_context_and_unqualified_name_semantics_remain_unknown(
    datatype: URIRef,
) -> None:
    """ASCII name support does not guess broader XML name or document context rules."""
    report = _emitted(datatype, "λname")
    assert not report.verified
    assert report.outcome == "unverified"
    assert "literal.datatype_unverified" in {issue.code for issue in report.issues}


def test_size_bound_and_custom_datatypes_stay_unverified() -> None:
    """Supported types retain finite lexical scope and unknown custom mappings."""
    assert (
        check_literal(Literal("0" * 4097, datatype=XSD.hexBinary, normalize=False))
        .issues[0]
        .code
        == "literal.lexical_unverified"
    )
    assert (
        check_literal(Literal("value", datatype=EX.Custom, normalize=False)).outcome
        == "unverified"
    )
    assert datatype_relation(str(XSD.duration), str(XSD.duration)) == "equal"
    assert datatype_relation(str(XSD.yearMonthDuration), str(XSD.duration)) == "unknown"
    assert datatype_relation(str(XSD.hexBinary), str(XSD.base64Binary)) == "unknown"
    assert datatype_relation(str(XSD.anyURI), str(XSD.string)) == "unknown"


@pytest.mark.parametrize(
    "datatype,valid,invalid",
    [(XSD.yearMonthDuration, "P1Y2M", "P0D"), (XSD.base64Binary, "Zg==", "Zh==")],
)
def test_jsonld_observed_literals_share_the_native_lexical_check(
    datatype: URIRef, valid: str, invalid: str
) -> None:
    """Actual JSON-LD source observations retain the same datatype and lexical truth."""
    for lexical, accepted in ((valid, True), (invalid, False)):
        document = {
            "@context": {"value": str(EX.value)},
            "@id": str(EX.record),
            "@type": str(EX.Record),
            "value": {"@value": lexical, "@type": str(datatype)},
        }
        result = check_emission(document, [_model(datatype)], [ModelUsage("record")])
        assert result.verified is accepted
        if not accepted:
            assert "literal.lexical_invalid" in {issue.code for issue in result.issues}
