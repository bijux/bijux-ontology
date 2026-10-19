"""Exact whole-property exclusions never waive losses at another source path."""

from copy import deepcopy
from typing import cast

import pytest

from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.selectors.types import ArrayItems, Property, SourceSelector
from bijux_ontology.jsonld.usages.checking import check_context, prepare_usages

EX = "https://example.org/"


def test_one_undefined_label_omission_does_not_waive_another_same_spelling() -> None:
    document = {
        "@context": {
            "optional": EX + "optional",
            "required": EX + "required",
            "kept": EX + "kept",
        },
        "optional": {"label": "optional loss"},
        "required": {"label": "required loss", "kept": "value"},
    }
    before = deepcopy(document)
    prepared = prepare_usages(document, allowed_omissions=(("optional", "label"),))
    report = prepared.check([ContextExpectation(("required", "kept"), EX + "kept")])
    assert prepared.allowed_omissions == (("optional", "label"),)
    assert [(issue.code, issue.actual, issue.location) for issue in report.issues] == [
        ("context.property_dropped", "label", "")
    ]
    assert report.exclusions == ("excluded source property: /optional/label",)
    assert not report.verified
    assert document == before
    assert prepared.observe(("required", "label")).dropped


def test_nested_array_leaf_exclusion_retains_values_and_explicit_scope() -> None:
    document = {
        "@context": {"@vocab": EX},
        "records": [
            {"label": "excluded", "title": "First"},
            {"label": "retained", "title": "Second"},
        ],
    }
    prepared = prepare_usages(document, allowed_omissions=(("records", 0, "label"),))
    report = prepared.check(
        [
            ContextExpectation(("records", 0, "title"), EX + "title"),
            ContextExpectation(("records", 1, "label"), EX + "label"),
        ]
    )
    assert report.verified
    assert report.checked == 2
    assert report.exclusions == ("excluded source property: /records/0/label",)
    assert prepared.observe(("records", 1, "label")).occurrences[0].value == {
        "@value": "retained"
    }


def test_excluded_expectations_never_count_as_verified_retained_content() -> None:
    prepared = prepare_usages(
        {"@context": {"@vocab": EX}, "label": "excluded"},
        allowed_omissions=(("label",),),
    )
    expected = [ContextExpectation(("label",), EX + "label")]
    required = prepared.check(expected)
    assert required.outcome == "disagreement"
    assert [issue.code for issue in required.issues] == [
        "context.required_scope_excluded"
    ]
    optional = prepared.check(expected, allow_empty=True)
    assert optional.outcome == "not_applicable" and optional.checked == 0
    assert not optional.verified


@pytest.mark.parametrize(
    "keyword", ["@id", "@type", "@graph", "@included", "@nest", "@list", "@set"]
)
def test_keyword_aliases_cannot_be_waived_as_ordinary_properties(keyword: str) -> None:
    value: object = (
        EX + "record"
        if keyword in {"@id", "@type"}
        else {"@id": EX + "record", EX + "title": "Value"}
    )
    document = {
        "@context": {"outer": EX + "outer", "alias": keyword, "kept": EX + "kept"},
        "outer": {"alias": value},
        "kept": "retained",
        "lost": "still lost",
    }
    report = check_context(
        document,
        [ContextExpectation(("kept",), EX + "kept")],
        allowed_omissions=(("outer", "alias"),),
    )
    assert not report.verified
    assert any(issue.code.startswith("context.omission_") for issue in report.issues)
    assert ("context.property_dropped", "lost") in {
        (issue.code, issue.actual) for issue in report.issues
    }
    assert not report.exclusions


@pytest.mark.parametrize(
    "path",
    [
        ("@context", "label"),
        ("@id",),
        ("@type",),
        ("records", 0),
        ("payload", "inside"),
    ],
)
def test_context_keywords_array_elements_and_opaque_interiors_fail_explicitly(
    path: tuple[str | int, ...],
) -> None:
    document = {
        "@context": {
            "@vocab": EX,
            "label": EX + "label",
            "payload": {"@id": EX + "payload", "@type": "@json"},
        },
        "@id": EX + "record",
        "@type": "Record",
        "records": [{"label": "Value"}],
        "payload": {"inside": "opaque"},
    }
    report = check_context(
        document,
        [ContextExpectation(("records", 0, "label"), EX + "label")],
        allowed_omissions=(path,),
    )
    assert report.outcome in {"input_error", "unverified"}
    assert any(issue.code.startswith("context.omission_") for issue in report.issues)
    assert not report.exclusions


def test_whole_opaque_property_is_allowed_and_interior_aliases_remain_protected() -> (
    None
):
    document = {
        "@context": {
            "@vocab": EX,
            "payload": {"@id": EX + "payload", "@type": "@json"},
        },
        "payload": {"inside": {"@context": {"x": "not a context"}}},
        "title": "Retained",
    }
    whole = check_context(
        document,
        [ContextExpectation(("title",), EX + "title")],
        allowed_omissions=(("payload",),),
    )
    assert whole.verified
    assert whole.exclusions == ("excluded source property: /payload",)


def test_unmatched_and_overlapping_contracts_are_atomic_and_retain_losses() -> None:
    document = {
        "@context": {"optional": EX + "optional", "kept": EX + "kept"},
        "optional": {"label": "lost"},
        "kept": "Value",
    }
    for paths in (
        (("optional", "label"), ("missing",)),
        (("optional",), ("optional", "label")),
    ):
        report = check_context(
            document,
            [ContextExpectation(("kept",), EX + "kept")],
            allowed_omissions=paths,
        )
        assert report.outcome == "input_error"
        assert not report.exclusions
        assert "context.property_dropped" in {issue.code for issue in report.issues}


def test_typed_selector_paths_reuse_exact_omission_contracts_without_new_language() -> (
    None
):
    document = {
        "@context": {"@vocab": EX},
        "records": [
            {"label": "First", "title": "One"},
            {"label": "Second", "title": "Two"},
        ],
    }
    source = prepare_usages(document)
    selected = source.select(
        SourceSelector(
            (Property("records"), ArrayItems(), Property("label", minimum=1))
        )
    )
    assert selected.report.verified
    retained = prepare_usages(document, allowed_omissions=selected.paths)
    report = retained.check(
        [
            ContextExpectation(("records", index, "title"), EX + "title")
            for index in range(2)
        ]
    )
    assert report.verified and len(report.exclusions) == 2


def test_null_mapped_occurrence_does_not_waive_unknown_callback_elsewhere() -> None:
    document = {
        "@context": {
            "optional": EX + "optional",
            "required": EX + "required",
            "label": None,
            "kept": EX + "kept",
        },
        "optional": {"label": "allowed"},
        "required": {"label": "not allowed", "kept": "Value"},
    }
    report = check_context(
        document,
        [ContextExpectation(("required", "kept"), EX + "kept")],
        allowed_omissions=(("optional", "label"),),
    )
    assert [(issue.code, issue.actual) for issue in report.issues] == [
        ("context.property_dropped", None)
    ]
    assert report.exclusions == ("excluded source property: /optional/label",)
    legacy = check_context(
        document,
        [ContextExpectation(("required", "kept"), EX + "kept")],
        allowed_drops=frozenset({"label"}),
    )
    assert not legacy.verified


def test_omission_probes_share_limits_and_do_not_change_input() -> None:
    document = {"@context": {"@vocab": EX}, "label": "excluded", "title": "retained"}
    before = deepcopy(document)
    report = check_context(
        document,
        [ContextExpectation(("title",), EX + "title")],
        allowed_omissions=(("label",),),
        limits=Limits(max_work=30),
    )
    assert report.outcome == "exhausted" and not report.complete
    assert document == before
    invalid = check_context(
        document, [], allowed_omissions=cast(tuple[tuple[str, ...], ...], [])
    )
    assert invalid.outcome == "input_error"


def test_reverse_property_exclusion_preserves_other_parent_facts() -> None:
    document = {
        "@context": {"@vocab": EX, "incoming": {"@reverse": EX + "related"}},
        "@id": EX + "record",
        "incoming": {"@id": EX + "other"},
        "title": "Retained",
    }
    report = check_context(
        document,
        [ContextExpectation(("title",), EX + "title")],
        allowed_omissions=(("incoming",),),
    )
    assert report.verified
    assert report.exclusions == ("excluded source property: /incoming",)
