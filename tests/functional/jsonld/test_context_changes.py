"""Context upgrade comparisons use representative processor meanings, not spelling."""

from copy import deepcopy

import pytest

from bijux_ontology.jsonld.comparison import (
    compare_context_usages,
    compare_serializations,
)
from bijux_ontology.limits import Limits

EX = "https://example.org/"


def test_scoped_mapping_change_identifies_usage_and_both_iris() -> None:
    before = {
        "@context": {
            "parent": {"@id": EX + "parent", "@context": {"name": EX + "old"}}
        },
        "parent": {"name": "Text"},
    }
    after = deepcopy(before)
    after["@context"] = {
        "parent": {"@id": EX + "parent", "@context": {"name": EX + "new"}}
    }
    report = compare_context_usages(before, after, [("parent", "name")])
    assert report.exit_code() == 1
    assert report.issues[0].location == "/parent/name"
    assert EX + "old" in str(report.issues[0].expected)
    assert EX + "new" in str(report.issues[0].actual)
    assert before["parent"] == {"name": "Text"}


@pytest.mark.parametrize(
    ("before_definition", "after_definition", "value"),
    [
        ({"@id": EX + "p", "@type": "@id"}, EX + "p", EX + "target"),
        (
            {"@id": EX + "p", "@language": "en"},
            {"@id": EX + "p", "@language": "fr"},
            "Text",
        ),
        ({"@id": EX + "p", "@container": "@list"}, EX + "p", ["left", "right"]),
    ],
)
def test_unchanged_predicate_does_not_hide_coercion_language_or_list_changes(
    before_definition: object,
    after_definition: object,
    value: object,
) -> None:
    report = compare_context_usages(
        {"@context": {"p": before_definition}, "p": value},
        {"@context": {"p": after_definition}, "p": value},
        [("p",)],
    )
    assert report.exit_code() == 1
    assert [issue.code for issue in report.issues] == ["context.usage_meaning_changed"]


def test_unchanged_meanings_ignore_unused_spelling_and_never_verify_unknown_scope() -> (
    None
):
    before = {"@context": {"name": EX + "name"}, "name": "Text"}
    after = {
        "@context": {"unused": EX + "unused", "name": {"@id": EX + "name"}},
        "name": "Text",
    }
    assert compare_context_usages(before, after, [("name",)]).verified
    ambiguous = {"@context": {"@vocab": EX}, "p": [{"name": "A"}, {"name": "B"}]}
    unknown = compare_context_usages(ambiguous, ambiguous, [("p", 0)])
    assert not unknown.verified
    assert unknown.issues[0].kind == "unverified"
    bounded = compare_context_usages(
        before, after, [("name",)], limits=Limits(max_work=1)
    )
    assert bounded.outcome == "exhausted"
    assert bounded.exit_code() == 2


def test_serialization_comparison_uses_rdf_list_and_blank_node_meaning() -> None:
    before = {
        "@context": {"p": {"@id": EX + "p", "@container": "@list"}},
        "@id": "_:original",
        "p": ["left", "right"],
    }
    equivalent = {"@id": "_:renamed", EX + "p": {"@list": ["left", "right"]}}
    assert compare_serializations(before, equivalent).verified
    reordered = {"@id": "_:renamed", EX + "p": {"@list": ["right", "left"]}}
    changed = compare_serializations(before, reordered)
    assert changed.exit_code() == 1
    assert "rdf.structure_changed" in {issue.code for issue in changed.issues}
    unordered = {EX + "p": ["left", "right"]}
    assert not compare_serializations(before, unordered).verified


def test_serialization_changes_retain_graph_scope_and_literal_coercion() -> None:
    before = {
        "@context": {"p": {"@id": EX + "p", "@type": "@id"}},
        "@id": EX + "graph",
        "@graph": [{"p": EX + "target"}],
    }
    graph_changed = deepcopy(before)
    graph_changed["@id"] = EX + "otherGraph"
    assert compare_serializations(before, graph_changed).exit_code() == 1
    literalized = deepcopy(before)
    literalized["@context"] = {"p": EX + "p"}
    assert compare_serializations(before, literalized).exit_code() == 1


def test_incomplete_conversion_cannot_establish_serialization_agreement() -> None:
    unsupported = {
        "@context": {"p": {"@id": EX + "p", "@type": "@id"}},
        "p": "relative",
    }
    report = compare_serializations(unsupported, unsupported)
    assert not report.verified
    assert "context.rdf_identifier_loss" in {issue.code for issue in report.issues}
    assert "context.serialization_unverified" in {issue.code for issue in report.issues}
    assert len(report.children) == 3
    bounded = compare_serializations(
        {EX + "p": "a"}, {EX + "p": "a"}, limits=Limits(max_work=1)
    )
    assert bounded.outcome == "exhausted"


def test_valid_interoperability_limits_stay_distinct_from_malformed_input() -> None:
    token = {
        EX + "p": {
            "@value": " a\n  b ",
            "@type": "http://www.w3.org/2001/XMLSchema#token",
        }
    }
    unsupported = compare_serializations(token, token)
    assert not unsupported.verified
    assert all(issue.kind != "input_error" for issue in unsupported.issues)
    malformed = compare_serializations({EX + "p": object()}, {EX + "p": "Text"})
    assert malformed.outcome == "input_error"
    empty_graph = {"@id": EX + "empty", "@graph": []}
    incomplete = compare_serializations(empty_graph, empty_graph)
    assert not incomplete.verified
    assert "context.rdf_empty_graph_loss" in {issue.code for issue in incomplete.issues}


def test_prepared_context_comparison_keeps_the_established_resource_snapshot() -> None:
    from bijux_ontology.jsonld.checking import prepare_usages

    current = EX + "label"

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {"@context": {"label": current}},
        }

    document = {"@context": EX + "context", "label": "Colour"}
    before = prepare_usages(document, document_loader=loader)
    after = prepare_usages(document, document_loader=loader)
    current = EX + "different"
    assert compare_context_usages(before, after, (("label",),)).verified
    overridden = compare_context_usages(before, after, (("label",),), after_base=EX)
    assert overridden.outcome == "input_error" and not overridden.verified
