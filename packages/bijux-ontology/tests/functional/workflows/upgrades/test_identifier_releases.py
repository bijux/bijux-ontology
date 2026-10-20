"""Actual used-identifier releases retain identity, raw truth and explicit proposals."""

from typing import Any, cast

import pytest
from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.accessions import AccessionRule, AccessionValue
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.registry import IdentifierRegistry, NamespaceContribution
from bijux_ontology.vocabulary.templates import IdentifierTemplate, TemplateKey
from bijux_ontology.workflows.upgrades.identifiers import (
    IdentifierEquivalence,
    IdentifierRelease,
    compare_identifier_releases,
)

OLD = "https://old.example/id/"
NEW = "https://new.example/id/"


def release(
    namespace: str = OLD,
    canonical: str = "P",
    pattern: str = "[0-9]+",
    *,
    rules: bool = True,
) -> IdentifierRelease:
    """Keep the old input alias permitted when changing canonical spelling."""
    registry = IdentifierRegistry(
        [NamespaceContribution(namespace, canonical, "caller", ("P",))]
    )
    return IdentifierRelease(
        "supplied", registry, (AccessionRule(namespace, pattern),) if rules else ()
    )


def tooling() -> None:
    """Require the maintained engine for positive grammar evidence."""
    pytest.importorskip("re2")


def test_mapping_change_is_not_erased_by_both_ontologies_containing_terms() -> None:
    tooling()
    value = AccessionValue("P:123", "curie", Location("application.json", ("id",)))
    for namespace in (OLD, NEW):
        graph = Graph().add((URIRef(namespace + "123"), RDF.type, OWL.Class))
        assert check_vocabulary(graph, [TermDeclaration(namespace + "123")]).verified
    checked = compare_identifier_releases(release(), release(NEW), [value])
    assert checked.verdict == "demonstrated-break"
    assert checked.before is not None and checked.before.report.verified
    assert checked.after is not None and checked.after.report.verified
    assert checked.uses[0].before_expansion == OLD + "123"
    assert checked.uses[0].after_expansion == NEW + "123"
    assert checked.uses[0].changes == ("expansion_changed",)
    assert checked.uses[0].original is value and checked.updates == ()
    assert any(i.details == value.location for i in checked.report.issues)


def test_spelling_only_and_unused_additions_remain_distinct() -> None:
    tooling()
    values = [AccessionValue("P:123", "curie")]
    spelling = compare_identifier_releases(release(), release(canonical="Q"), values)
    assert spelling.verdict == "informational"
    assert spelling.uses[0].changes == ("canonical_spelling_changed",)
    assert spelling.uses[0].before_expansion == spelling.uses[0].after_expansion
    assert spelling.updates == ()
    extra = IdentifierRelease(
        "added",
        IdentifierRegistry(
            [
                NamespaceContribution(OLD, "P", "caller"),
                NamespaceContribution("urn:unused:", "unused", "caller"),
            ]
        ),
        (AccessionRule(OLD, "[0-9]+"), AccessionRule("urn:unused:", "[A-Z]+")),
    )
    same = compare_identifier_releases(release(), extra, values)
    assert same.report.verified and same.verdict == "agreement"
    assert same.uses[0].changes == ()


def test_actual_validity_transitions_and_unknown_baseline_never_compatible() -> None:
    tooling()
    values = [
        AccessionValue("P:12", "curie"),
        AccessionValue("P:bad", "curie"),
        AccessionValue("P:123", "curie"),
    ]
    strict = release(pattern="[0-9]{3}")
    checked = compare_identifier_releases(release(), strict, values)
    assert [item.validation for item in checked.uses] == [
        "introduced",
        "persistent",
        "unchanged",
    ]
    assert checked.verdict == "demonstrated-break"
    repaired = compare_identifier_releases(strict, release(), values)
    assert [item.validation for item in repaired.uses] == [
        "resolved",
        "persistent",
        "unchanged",
    ]
    assert (
        repaired.before is not None and repaired.before.report.outcome == "disagreement"
    )
    unknown = compare_identifier_releases(release(rules=False), release(), values[:1])
    assert unknown.verdict == "unverified" and not unknown.report.verified
    assert unknown.uses[0].validation == "uncertain"


def test_real_alternates_new_ambiguity_and_unavailable_profiles_are_not_conflated() -> (
    None
):
    tooling()
    pytest.importorskip("uri_template")
    template = IdentifierTemplate(
        "https://shared.example/{local}", [TemplateKey("local")]
    )
    unsupported = IdentifierTemplate(
        "https://shared.example/{+local}", [TemplateKey("local")]
    )
    first = AccessionRule(OLD, "[0-9]+", alternates=(template,))
    before = IdentifierRelease("before", release().registry, (first,))
    registry = IdentifierRegistry(
        [
            NamespaceContribution(OLD, "P", "caller"),
            NamespaceContribution(NEW, "Q", "caller"),
        ]
    )
    after = IdentifierRelease(
        "after", registry, (first, AccessionRule(NEW, "[0-9]+", alternates=(template,)))
    )
    value = AccessionValue(URIRef("https://shared.example/123"), "iri")
    checked = compare_identifier_releases(before, after, [value])
    assert checked.verdict == "demonstrated-break"
    assert checked.uses[0].changes == ("recognition_became_ambiguous",)
    assert (
        checked.before is not None and checked.before.observations[0].ambiguous is False
    )
    assert checked.after is not None and checked.after.observations[0].ambiguous is True
    assert checked.uses[0].before_expansion == str(value.value)
    assert checked.uses[0].original.value is value.value
    unresolved = IdentifierRelease(
        "after",
        registry,
        (first, AccessionRule(NEW, "[0-9]+", alternates=(unsupported,))),
    )
    uncertain = compare_identifier_releases(before, unresolved, [value])
    assert uncertain.verdict == "unverified" and uncertain.uses[0].changes == ()
    assert (
        uncertain.after is not None
        and uncertain.after.observations[0].ambiguous is None
    )


def test_proposals_require_explicit_exact_pairs_and_actual_after_acceptance() -> None:
    tooling()
    value = AccessionValue("P:123", "curie")
    pair = IdentifierEquivalence(URIRef(OLD + "123"), URIRef(NEW + "123"))
    checked = compare_identifier_releases(
        release(), release(NEW, "Q"), [value], equivalences=[pair]
    )
    assert checked.verdict == "demonstrated-break" and value.value == "P:123"
    assert checked.updates[0].admissible is True
    assert checked.updates[0].candidate == AccessionValue("Q:123", "curie")
    assert (
        checked.updates[0].after is not None
        and checked.updates[0].after.report.verified
    )
    no_pair = compare_identifier_releases(release(), release(canonical="Q"), [value])
    assert no_pair.updates == ()
    reflexive = IdentifierEquivalence(pair.before, pair.before)
    spelling = compare_identifier_releases(
        release(), release(canonical="Q"), [value], equivalences=[reflexive]
    )
    assert spelling.updates[0].candidate == AccessionValue("Q:123", "curie")
    invalid = compare_identifier_releases(
        release(),
        release(NEW),
        [value],
        equivalences=[IdentifierEquivalence(pair.before, URIRef(NEW + "bad"))],
    )
    assert invalid.updates[0].admissible is False
    missing = compare_identifier_releases(
        release(),
        release(NEW),
        [value],
        equivalences=[IdentifierEquivalence(pair.before, URIRef("urn:outside:123"))],
    )
    assert (
        missing.updates[0].admissible is None and missing.updates[0].candidate is None
    )
    conflict = compare_identifier_releases(
        release(),
        release(NEW),
        [value],
        equivalences=[pair, IdentifierEquivalence(pair.before, URIRef(NEW + "456"))],
    )
    assert conflict.report.outcome == "input_error" and conflict.updates == ()
    iri_value = AccessionValue(pair.before, "iri")
    iri = compare_identifier_releases(
        release(), release(NEW), [iri_value], equivalences=[pair]
    )
    assert iri.updates[0].candidate == AccessionValue(pair.after, "iri")
    assert iri.uses[0].before_expansion == str(pair.before)


def test_bounds_raw_prefix_and_required_input_scope() -> None:
    tooling()
    bad = AccessionValue("P:bad", "curie", Location("out.json", ("a/b",)))
    budget = Budget(Limits(max_issues=1))
    stopped = compare_identifier_releases(
        release(), release(), [bad, bad], budget=budget
    )
    assert stopped.report.outcome == "exhausted"
    assert stopped.before is not None
    assert any(
        i.code == "vocabulary.accession_invalid" for i in stopped.before.report.issues
    )
    assert any(i.code == "vocabulary.accession_invalid" for i in stopped.report.issues)
    assert not compare_identifier_releases(release(), release(), []).report.verified
    giant = IdentifierRelease("é" * 100000, release().registry, release().rules)
    metadata = compare_identifier_releases(
        giant, release(), [bad], limits=Limits(max_bytes=1000)
    )
    assert metadata.report.outcome == "exhausted" and metadata.before is None
    for term in (BNode("urn:id"), Literal("urn:id")):
        with pytest.raises(InputError):
            IdentifierEquivalence(cast(URIRef, term), URIRef("urn:target"))
    invalid = compare_identifier_releases(release(), release(), cast(Any, ["P:123"]))
    assert invalid.report.outcome == "input_error"
    assert compare_identifier_releases(
        release(), release(), [AccessionValue("P:123", "curie")]
    ).report.verified


def test_genuine_core_absence_leaves_both_sides_unknown() -> None:
    import importlib.util

    if importlib.util.find_spec("re2") is not None:
        pytest.skip("Requires genuine core installation")
    checked = compare_identifier_releases(
        release(), release(), [AccessionValue("P:123", "curie")]
    )
    assert checked.verdict == "unverified" and not checked.report.verified
    assert checked.before is not None and checked.before.report.outcome == "unverified"
    assert checked.after is not None and checked.after.report.outcome == "unverified"
