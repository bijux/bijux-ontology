"""Explicit field associations retain mapping and joint resource topology."""

from dataclasses import replace

from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.comparison import compare_observed_usages
from bijux_ontology.limits import Budget, Limits


def test_renamed_source_paths_use_explicit_correspondence_without_reexpansion() -> None:
    before = prepare_usages({"@context": {"old": "urn:example:p"}, "old": "A"})
    after = prepare_usages({"@context": {"new": "urn:example:p"}, "new": "B"})
    old, new = before.observe(("old",)), after.observe(("new",))
    result = compare_observed_usages((("consumer-field", old, new),), budget=Budget())
    assert result.verified  # Literal contents belong to the separate RDF comparison.
    assert old.path == ("old",) and new.path == ("new",)
    assert old.occurrences[0].location == "/old"
    assert new.occurrences[0].location == "/new"
    changed = prepare_usages({"@context": {"new": "urn:example:q"}, "new": "B"})
    result = compare_observed_usages(
        (("consumer-field", old, changed.observe(("new",))),), budget=Budget()
    )
    assert result.outcome == "disagreement"
    assert result.issues[0].location == "consumer-field"


def test_separate_field_associations_preserve_shared_blank_node_identity() -> None:
    def document(left: str, right: str) -> dict[str, object]:
        return {
            "@context": {
                "a": {"@id": "urn:example:a", "@type": "@id"},
                "b": {"@id": "urn:example:b", "@type": "@id"},
            },
            "@id": "urn:example:record",
            "a": left,
            "b": right,
        }

    before = prepare_usages(document("_:shared", "_:shared"))
    after = prepare_usages(document("_:left", "_:right"))
    pairs = tuple(
        (name, before.observe((name,)), after.observe((name,))) for name in ("a", "b")
    )
    assert compare_observed_usages(pairs, budget=Budget()).outcome == "disagreement"
    renamed = prepare_usages(document("_:other", "_:other"))
    pairs = tuple(
        (name, before.observe((name,)), renamed.observe((name,))) for name in ("a", "b")
    )
    assert compare_observed_usages(pairs, budget=Budget()).verified


def test_unproven_locations_and_bounded_evidence_cannot_certify_agreement() -> None:
    prepared = prepare_usages({"@context": {"p": "urn:example:p"}, "p": "value"})
    usage = prepared.observe(("p",))
    foreign = replace(
        usage, occurrences=(replace(usage.occurrences[0], location="/unrelated"),)
    )
    assert (
        compare_observed_usages((("field", usage, foreign),), budget=Budget()).outcome
        == "unverified"
    )
    for limits in (Limits(max_bytes=1), Limits(max_states=1), Limits(max_work=1)):
        assert (
            compare_observed_usages(
                (("field", usage, usage),), budget=Budget(limits)
            ).outcome
            == "exhausted"
        )


def test_explicit_comparison_budget_does_not_charge_an_old_preparation() -> None:
    from bijux_ontology.jsonld.comparison import compare_context_usages

    original = Budget()
    prepared = prepare_usages(
        {"@context": {"p": "urn:example:p"}, "p": "value"}, budget=original
    )
    before = dict(original._counts)
    report = compare_context_usages(
        prepared, prepared, (("p",),), budget=Budget(Limits(max_work=1))
    )
    assert not report.verified and original._counts == before
    prepared.observe(("p",))
    before = dict(original._counts)
    assert compare_context_usages(
        prepared, prepared, (("p",),), budget=Budget()
    ).verified
    assert original._counts == before
