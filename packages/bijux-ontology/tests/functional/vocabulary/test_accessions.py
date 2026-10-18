"""Actual supplied accessions retain spelling, source location and bounded scope."""

from dataclasses import replace
from typing import Any, cast

import pytest
from rdflib import BNode, Literal, URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.reports import Location
from bijux_ontology.vocabulary.accessions import (
    AccessionRule,
    AccessionValue,
    check_accessions,
)
from bijux_ontology.vocabulary.registry import (
    IdentifierRegistry,
    NamespaceContribution,
)
from bijux_ontology.vocabulary.templates import IdentifierTemplate, TemplateKey

NS = "https://orcid.org/"
GRAMMAR = r"[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]"


def registry() -> IdentifierRegistry:
    """Declare aliases and nested namespaces without a service lookup."""
    return IdentifierRegistry(
        [NamespaceContribution(NS, "ORCID", "caller", ("orcid",))]
    )


def rule(*, alternate: bool = True) -> AccessionRule:
    """Use the official public ORCID checksum examples and exact ASCII grammar."""
    alternatives = (
        (IdentifierTemplate("http://orcid.org/{local}", [TemplateKey("local")]),)
        if alternate
        else ()
    )
    return AccessionRule(
        NS,
        GRAMMAR,
        case_policy="ascii-upper",
        check_digit="iso7064-mod11-2",
        checksum_separators="-",
        alternates=alternatives,
    )


def tooling() -> None:
    """Require real maintained processors only for their actual positive scopes."""
    pytest.importorskip("re2")
    pytest.importorskip("stdnum")


def test_official_accessions_curie_alternate_mutations_and_original_occurrences() -> (
    None
):
    tooling()
    pytest.importorskip("uri_template")
    document = {
        "creator": [
            "orcid:0000-0002-1825-0097",
            "http://orcid.org/0000-0002-1825-0097",
            "ORCID:0000-0002-1825-0098",
            "ORCID:0000-0002-1825-00A7",
        ]
    }
    values = tuple(
        AccessionValue(
            value,
            "iri" if index == 1 else "curie",
            Location("output.json", ("creator", index)),
        )
        for index, value in enumerate(document["creator"])
    )
    checked = check_accessions(values, registry(), rules=[rule()])
    assert checked.report.outcome == "disagreement"
    assert [value.status for value in checked.observations] == [
        "valid",
        "valid",
        "invalid",
        "invalid",
    ]
    assert [issue.location for issue in checked.report.issues] == [
        "output.json/creator/2",
        "output.json/creator/3",
    ]
    assert all(
        observation.original is original
        for observation, original in zip(checked.observations, values, strict=True)
    )
    assert all(
        observation.canonical_curie == "ORCID:0000-0002-1825-0097"
        for observation in checked.observations[:2]
    )
    assert document["creator"][1].startswith("http:")
    accepted = check_accessions(values[:2], registry(), rules=[rule()])
    assert accepted.report.verified
    exact = URIRef(NS + "0000-0002-1694-233X")
    iri = check_accessions([AccessionValue(exact, "iri")], registry(), rules=[rule()])
    assert iri.report.verified and iri.observations[0].original.value is exact
    typed = IdentifierRegistry(
        [NamespaceContribution(URIRef(NS), URIRef("ORCID"), "caller")]
    )
    assert (
        check_accessions(
            values[:1], typed, rules=[rule(alternate=False)]
        ).report.verified
        is False
    )
    assert check_accessions(
        [AccessionValue("ORCID:0000-0002-1825-0097", "curie")],
        typed,
        rules=[rule(alternate=False)],
    ).report.verified


def test_case_hints_are_not_rewrites_and_checksums_have_explicit_profiles() -> None:
    tooling()
    original = AccessionValue("ORCID:0000-0002-1694-233x", "curie")
    result = check_accessions([original], registry(), rules=[rule(alternate=False)])
    assert result.report.outcome == "disagreement"
    assert result.observations[0].suggested_curie == "ORCID:0000-0002-1694-233X"
    assert result.observations[0].canonical_curie is None
    assert original.value.endswith("x")
    invalid = check_accessions(
        [replace(original, value="ORCID:0000-0002-1694-234x")],
        registry(),
        rules=[rule(alternate=False)],
    )
    assert (
        invalid.report.outcome == "disagreement"
        and invalid.observations[0].suggested_curie is None
    )
    digits = IdentifierRegistry([NamespaceContribution("urn:number:", "N", "caller")])
    luhn = AccessionRule("urn:number:", r"[0-9]+", check_digit="luhn")
    assert check_accessions(
        [AccessionValue("N:79927398713", "curie")], digits, rules=[luhn]
    ).report.verified
    assert not check_accessions(
        [AccessionValue("N:79927398714", "curie")], digits, rules=[luhn]
    ).report.verified
    broad = replace(luhn, pattern=".*")
    assert (
        check_accessions(
            [AccessionValue("N:٧9927398713", "curie")], digits, rules=[broad]
        ).report.outcome
        == "disagreement"
    )
    with pytest.raises(InputError, match="punctuation"):
        replace(luhn, checksum_separators="1")


def test_unknown_namespaces_explicit_intent_nested_and_ambiguous_templates() -> None:
    tooling()
    pytest.importorskip("uri_template")
    simple = rule(alternate=False)
    unknown = AccessionValue("Other:0000-0002-1825-0097", "curie")
    assert (
        check_accessions([unknown], registry(), rules=[simple]).report.outcome
        == "unverified"
    )
    excluded = check_accessions(
        [unknown], registry(), rules=[simple], unknown_namespaces="exclude"
    )
    assert not excluded.report.verified and excluded.report.exclusions
    assert excluded.observations[0].status == "excluded"
    for mode, spelling in (
        ("curie", "ORCID:0000-0002-1825-0097"),
        ("iri", NS + "0000-0002-1825-0097"),
    ):
        missing_rule = check_accessions(
            [AccessionValue(spelling, cast(Any, mode))],
            registry(),
            rules=[],
            unknown_namespaces="exclude",
        )
        assert missing_rule.report.outcome == "unverified"
        assert not missing_rule.report.exclusions
    mixed = check_accessions(
        [unknown, AccessionValue("ORCID:0000-0002-1825-0097", "curie")],
        registry(),
        rules=[simple],
        unknown_namespaces="exclude",
    )
    assert (
        mixed.report.verified
        and mixed.report.checked == 1
        and mixed.report.requested == 1
    )
    urn = IdentifierRegistry(
        [
            NamespaceContribution("urn:expanded:", "urn", "caller"),
            NamespaceContribution("urn:expanded:nested:", "child", "caller"),
        ]
    )
    urn_rules = [
        AccessionRule("urn:expanded:", ".+"),
        AccessionRule("urn:expanded:nested:", "[0-9]+"),
    ]
    assert check_accessions(
        [AccessionValue("urn:7", "curie")], urn, rules=urn_rules
    ).report.verified
    assert not check_accessions(
        [AccessionValue("urn:7", "iri")], urn, rules=urn_rules
    ).report.verified
    nested = check_accessions(
        [AccessionValue("urn:expanded:nested:7", "iri")], urn, rules=urn_rules
    )
    assert (
        nested.report.verified and nested.observations[0].canonical_curie == "child:7"
    )
    alternatives = IdentifierRegistry(
        [
            NamespaceContribution(NS, "ORCID", "a"),
            NamespaceContribution("urn:other:", "O", "b"),
        ]
    )
    a = rule()
    b = AccessionRule("urn:other:", ".*", alternates=a.alternates)
    ambiguous = check_accessions(
        [AccessionValue("http://orcid.org/0000-0002-1825-0097", "iri")],
        alternatives,
        rules=[a, b],
        unknown_namespaces="exclude",
    )
    assert ambiguous.report.outcome == "unverified" and not ambiguous.report.exclusions
    unsupported = replace(
        a,
        alternates=(
            IdentifierTemplate("http://orcid.org/{+local}", [TemplateKey("local")]),
        ),
    )
    assert (
        check_accessions(
            [AccessionValue(NS + "0000-0002-1825-0097", "iri")],
            registry(),
            rules=[unsupported],
        ).report.outcome
        == "unverified"
    )


def test_bounds_quotas_prefixes_and_no_kind_erasure() -> None:
    tooling()
    policy = rule(alternate=False)
    bad = AccessionValue("ORCID:bad", "curie", Location("output.json", ("a/b",)))
    budget = Budget(Limits(max_issues=1))
    prefix = check_accessions([bad, bad], registry(), rules=[policy], budget=budget)
    assert prefix.report.outcome == "exhausted"
    assert prefix.report.issues[0].code == "vocabulary.accession_invalid"
    assert prefix.report.issues[0].location == "output.json/a~1b"
    assert (
        check_accessions(
            [bad], registry(), rules=[policy], budget=budget
        ).report.outcome
        == "exhausted"
    )
    assert (
        check_accessions([bad], registry(), rules=[policy]).report.outcome
        == "disagreement"
    )
    giant = AccessionValue("ORCID:" + "x" * 100_000, "curie")
    cap = Limits(max_bytes=1000, max_total_bytes=10000)
    bounded = check_accessions([giant], registry(), rules=[policy], limits=cap)
    assert bounded.report.outcome == "exhausted" and not bounded.observations
    long_namespace = "https://example.org/" + "x" * 650 + "/"
    expansion = check_accessions(
        [AccessionValue("P:" + "x" * 500, "curie")],
        IdentifierRegistry([NamespaceContribution(long_namespace, "P", "caller")]),
        rules=[AccessionRule(long_namespace, ".*")],
        limits=cap,
    )
    assert expansion.report.outcome == "exhausted" and not expansion.observations
    for location in [Location("é" * 250, ("é" * 250,)), Location(path=(10**10000,))]:
        limited = check_accessions(
            [replace(bad, location=location)], registry(), rules=[policy], limits=cap
        )
        assert limited.report.outcome == "exhausted" and not limited.observations
    for kind in (BNode, Literal):
        with pytest.raises(InputError):
            AccessionValue(kind("urn:value"), "iri")
        with pytest.raises(InputError):
            AccessionRule(kind("urn:namespace:"), ".*")
    with pytest.raises(InputError):
        check_accessions(
            [bad], registry(), rules=[policy], limits=Limits(), budget=Budget(cap)
        )
    invalid = check_accessions(cast(Any, ["not typed"]), registry(), rules=[policy])
    assert invalid.report.outcome == "input_error"
    malformed = check_accessions(
        [AccessionValue("missing prefix", "curie", bad.location)],
        registry(),
        rules=[policy],
    )
    assert malformed.report.outcome == "disagreement"
    assert malformed.report.issues[0].location == "output.json/a~1b"
    assert malformed.report.issues[0].actual == "missing prefix"


def test_missing_optional_processors_never_supply_positive_evidence() -> None:
    """The genuine core consumer exercises this same ordinary public endpoint."""
    import importlib.util

    if importlib.util.find_spec("re2") is not None:
        pytest.skip("Requires the genuine core installation")
    result = check_accessions(
        [AccessionValue("ORCID:0000-0002-1825-0097", "curie")],
        registry(),
        rules=[rule(alternate=False)],
    )
    assert result.report.outcome == "unverified" and not result.report.verified
    assert result.observations[0].status == "unknown"


def test_ambiguity_is_actual_distinct_records_not_unavailable_recognition() -> None:
    pytest.importorskip("uri_template")
    pytest.importorskip("re2")
    template = IdentifierTemplate(
        "https://shared.example/{local}", [TemplateKey("local")]
    )
    unavailable = IdentifierTemplate(
        "https://shared.example/{+local}", [TemplateKey("local")]
    )
    namespaces = ("urn:first:", "urn:second:", "urn:third:")
    owned = IdentifierRegistry(
        [
            NamespaceContribution(value, f"p{index}", "caller")
            for index, value in enumerate(namespaces)
        ]
    )
    term = URIRef("https://shared.example/abc")
    values = [AccessionValue(term, "iri")]
    rules = [
        AccessionRule(value, "[a-z]+", alternates=(template,))
        for value in namespaces[:2]
    ]
    unique = check_accessions(values, owned, rules=rules[:1])
    assert unique.report.verified and unique.observations[0].ambiguous is False
    assert unique.observations[0].original.value is term
    ambiguous = check_accessions(values, owned, rules=rules)
    assert (
        ambiguous.report.outcome == "unverified"
        and ambiguous.observations[0].ambiguous is True
    )
    assert ambiguous.observations[0].original.value is term
    partial = AccessionRule(namespaces[2], "[a-z]+", alternates=(unavailable,))
    assert (
        check_accessions(values, owned, rules=[*rules, partial])
        .observations[0]
        .ambiguous
        is True
    )
    unresolved = check_accessions(values, owned, rules=[rules[0], partial])
    assert (
        unresolved.report.outcome == "unverified"
        and unresolved.observations[0].ambiguous is None
    )
