"""Datatype distinctions and actual sample lexical forms."""

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.literals import check_literal, datatype_relation


def test_value_space_relations_are_directional_and_regime_explicit() -> None:
    assert datatype_relation(str(XSD.byte), str(XSD.integer)) == "subset"
    assert datatype_relation(str(XSD.integer), str(XSD.byte)) == "overlap"
    assert (
        datatype_relation(str(XSD.positiveInteger), str(XSD.negativeInteger))
        == "disjoint"
    )
    assert (
        datatype_relation(str(XSD.nonPositiveInteger), str(XSD.nonNegativeInteger))
        == "overlap"
    )
    assert datatype_relation(str(XSD.token), str(XSD.string)) == "subset"
    assert datatype_relation(str(XSD.float), str(XSD.decimal)) == "unknown"
    assert (
        datatype_relation(str(XSD.float), str(XSD.decimal), regime="owl-dl")
        == "disjoint"
    )
    assert (
        datatype_relation("https://example.org/Custom", "https://example.org/Custom")
        == "unknown"
    )


@pytest.mark.parametrize(
    "datatype,valid,invalid",
    [
        (XSD.integer, "+0001", "1.2"),
        (XSD.byte, "127", "128"),
        (XSD.decimal, ".125", "1e3"),
        (XSD.boolean, "1", "yes"),
        (XSD.double, "-INF", "infinity"),
        (XSD.date, "2024-02-29+14:00", "2023-02-29"),
        (XSD.dateTime, "2024-12-31T24:00:00Z", "2024-01-01T24:00:01Z"),
        (XSD.dateTimeStamp, "2024-01-01T00:00:00-01:00", "2024-01-01T00:00:00"),
    ],
)
def test_sample_lexical_validation_preserves_identity(
    datatype: URIRef, valid: str, invalid: str
) -> None:
    accepted = Literal(valid, datatype=datatype, normalize=False)
    rejected = Literal(invalid, datatype=datatype, normalize=False)
    before = str(accepted), str(rejected)
    assert check_literal(accepted).verified
    assert check_literal(rejected).issues[0].code == "literal.lexical_invalid"
    assert (str(accepted), str(rejected)) == before


def test_language_and_unsupported_datatypes_retain_honest_scope() -> None:
    assert check_literal(Literal("bonjour", lang="fr")).verified
    assert check_literal(Literal("abcdefgh", datatype=XSD.language)).verified
    assert not check_literal(Literal("no language", datatype=RDF.langString)).verified
    report = check_literal(Literal("value", datatype="https://example.org/Custom"))
    assert report.issues[0].kind == "unverified"


def test_sample_bounds_precede_validation_and_share_the_callers_allowance() -> None:
    """Exact literal fields and diagnostics cannot bypass ordinary operation limits."""
    for term, location in (
        (Literal("x" * 2000, normalize=False), ""),
        (Literal("é" * 20, normalize=False), ""),
        (Literal("x", datatype="urn:datatype:" + "x" * 2000), ""),
        (Literal("x", normalize=False), "x" * 2000),
    ):
        result = check_literal(
            term,
            location=location,
            budget=Budget(Limits(max_bytes=32, max_total_bytes=128)),
        )
        assert result.outcome == "exhausted" and not result.complete
        assert all(issue.actual is None for issue in result.issues)

    term = Literal("+0001", datatype=XSD.integer, normalize=False)
    before = str(term), term.datatype, term.language
    shared = Budget(Limits(max_work=80))
    assert check_literal(term, budget=shared).verified
    assert check_literal(term, budget=shared).outcome == "exhausted"
    assert check_literal(term, budget=Budget()).verified
    assert (str(term), term.datatype, term.language) == before
    invalid = check_literal(
        Literal("128", datatype=XSD.byte, normalize=False), budget=Budget()
    )
    unsupported = check_literal(
        Literal("x", datatype="urn:datatype:unavailable"), budget=Budget()
    )
    assert invalid.outcome == "disagreement"
    assert unsupported.outcome == "unverified"
