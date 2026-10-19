"""Context upgrade comparisons use representative processor meanings, not spelling."""

import json
from copy import deepcopy
from typing import cast

import pytest

from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.usages.comparison import (
    compare_context_usages,
    compare_serializations,
)

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
    exact = compare_serializations(token, token)
    assert exact.verified
    normalized = {EX + "p": {"@value": "a b", "@type": token[EX + "p"]["@type"]}}
    assert compare_serializations(token, normalized).outcome == "disagreement"
    malformed = compare_serializations({EX + "p": object()}, {EX + "p": "Text"})
    assert malformed.outcome == "input_error"
    empty_graph = {"@id": EX + "empty", "@graph": []}
    incomplete = compare_serializations(empty_graph, empty_graph)
    assert not incomplete.verified
    assert "context.rdf_empty_graph_loss" in {issue.code for issue in incomplete.issues}


def test_prepared_context_comparison_keeps_the_established_resource_snapshot() -> None:
    from bijux_ontology.jsonld.usages.checking import prepare_usages

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


@pytest.mark.parametrize("lists", [[["A"], ["B"]], [[], ["A"]]])
def test_usage_comparison_preserves_list_boundaries_without_array_order_claims(
    lists: list[list[str]],
) -> None:
    before = {"@context": {"p": EX + "p"}, "p": [{"@list": values} for values in lists]}
    reordered = {
        "@context": {"p": EX + "p"},
        "p": [{"@list": values} for values in reversed(lists)],
    }
    assert compare_context_usages(before, reordered, [("p",)]).verified
    merged = {
        "@context": {"p": EX + "p"},
        "p": {"@list": [value for values in lists for value in values]},
    }
    report = compare_context_usages(before, merged, [("p",)])
    assert report.outcome == "disagreement"
    assert [issue.code for issue in report.issues] == ["context.usage_meaning_changed"]


def test_rdf_nil_is_shared_while_nonempty_list_heads_remain_distinct() -> None:
    for members, expected in (([], "agreement"), (["A"], "disagreement")):
        single = {EX + "p": {"@list": members}}
        repeated = {EX + "p": [{"@list": members}, {"@list": members}]}
        assert compare_serializations(single, repeated).outcome == expected
        assert (
            compare_context_usages(single, repeated, [(EX + "p",)]).outcome == expected
        )


@pytest.mark.parametrize("keyword", ["@nest", "@included"])
@pytest.mark.parametrize("outer_graph", [False, True])
def test_selected_field_cannot_silently_leave_an_anonymous_named_graph(
    keyword: str,
    outer_graph: bool,
) -> None:
    context = {"@vocab": EX, "scope": "@graph"}
    child = {"box": {"scope": {"p": "Text"}}}
    before = {"@context": context, **child}
    path: tuple[str | int, ...] = ("box", "scope", "p")
    if outer_graph:
        before = {"@context": context, "@id": EX + "outer", "@graph": [child]}
        path = ("@graph", 0, *path)
    after = deepcopy(before)
    after["@context"] = {"@vocab": EX, "scope": keyword}
    original = deepcopy(before)
    report = compare_context_usages(before, after, [path])
    assert report.outcome == "disagreement"
    assert [issue.code for issue in report.issues] == ["context.usage_meaning_changed"]
    assert compare_serializations(before, after).outcome == "disagreement"
    assert before == original


def test_graph_wrappers_and_blank_graph_labels_keep_equivalent_scope() -> None:
    before = {
        "@context": {"@vocab": EX, "scope": "@graph"},
        "box": {"scope": {"p": "Text"}},
    }
    labelled = deepcopy(before)
    labelled["box"] = {"@id": "_:graph", "scope": {"p": "Text"}}
    assert compare_context_usages(before, labelled, [("box", "scope", "p")]).verified
    assert compare_serializations(before, labelled).verified
    wrapper = {"@context": {"@vocab": EX, "scope": "@graph"}, "scope": {"p": "Text"}}
    nested = {"@context": {"@vocab": EX, "scope": "@nest"}, "scope": {"p": "Text"}}
    assert compare_context_usages(wrapper, nested, [("scope", "p")]).verified
    assert compare_serializations(wrapper, nested).verified


@pytest.mark.parametrize(
    ("document", "selectors"),
    [
        (
            {
                "@context": {
                    "p": {"@id": EX + "p", "@type": "@vocab"},
                    "Target": "_:second",
                },
                "@id": "_:first",
                "p": "Target",
            },
            [("p",)],
        ),
        (
            {
                "@id": EX + "root",
                EX + "p": {"@id": "_:first"},
                EX + "q": {"@id": "_:second"},
            },
            [(EX + "p",), (EX + "q",)],
        ),
        (
            {
                "@graph": [
                    {"@id": "_:first", EX + "p": "one"},
                    {"@id": "_:second", EX + "q": "two"},
                ]
            },
            [("@graph", 0, EX + "p"), ("@graph", 1, EX + "q")],
        ),
        (
            {
                EX + "first": {
                    "@id": "_:first",
                    "@graph": {"@id": EX + "s", EX + "p": "one"},
                },
                EX + "second": {
                    "@id": "_:second",
                    "@graph": {"@id": EX + "t", EX + "p": "two"},
                },
            },
            [(EX + "first", "@graph", EX + "p"), (EX + "second", "@graph", EX + "p")],
        ),
        (
            {
                "@id": EX + "root",
                EX + "p": {"@id": "_:first", "@type": "_:second"},
            },
            [(EX + "p",)],
        ),
        (
            {
                "@id": "_:first",
                EX + "p": {"@list": [{"@id": "_:first"}, {"@id": "_:second"}]},
            },
            [(EX + "p",)],
        ),
    ],
    ids=("self-loop", "referents", "subjects", "graphs", "types", "lists"),
)
def test_selected_resource_identity_uses_one_global_blank_correspondence(
    document: object, selectors: list[tuple[str | int, ...]]
) -> None:
    encoded = json.dumps(document)
    renamed = json.loads(encoded.replace("_:", "_:renamed-"))
    assert compare_context_usages(document, renamed, selectors).verified

    merged = json.loads(encoded.replace("_:second", "_:first"))
    for before, after in ((document, merged), (merged, document)):
        report = compare_context_usages(before, after, selectors)
        assert report.complete and report.outcome == "disagreement"
        assert {issue.code for issue in report.issues} == {
            "context.usage_meaning_changed"
        }
    iri_document = json.loads(encoded.replace("_:", EX))
    iri_merged = json.loads(encoded.replace("_:second", "_:first").replace("_:", EX))
    assert (
        compare_context_usages(iri_document, iri_merged, selectors).outcome
        == "disagreement"
    )


def test_selected_topology_does_not_compare_unselected_assertions() -> None:
    before = {"@id": "_:root", EX + "p": {"@id": "_:other"}, EX + "q": "text"}
    after = {"@id": "_:renamed", EX + "p": {"@id": "_:target"}, EX + "q": 7}
    assert compare_context_usages(before, after, [(EX + "p",)]).verified
    assert compare_serializations(before, after).outcome == "disagreement"


def test_implicit_subjects_and_graphs_keep_distinct_selected_identities() -> None:
    before = {
        "@context": {"@vocab": EX, "box": EX + "box"},
        "p": "one",
        "box": {"q": "two"},
    }
    after = deepcopy(before)
    cast(dict[str, str], after["@context"])["box"] = "@nest"
    selectors = [("p",), ("box", "q")]
    assert compare_context_usages(before, before, selectors).verified
    labeled = deepcopy(before)
    labeled["@id"] = "_:root"
    cast(dict[str, str], labeled["box"])["@id"] = "_:child"
    assert compare_context_usages(before, labeled, [*selectors, ("box",)]).verified
    assert compare_context_usages(before, after, selectors).outcome == "disagreement"

    separate = {
        "@context": {"@vocab": EX, "nodes": "@graph"},
        "first": {"nodes": {"@id": EX + "s", "p": "one"}},
        "second": {"nodes": {"@id": EX + "t", "p": "two"}},
    }
    shared = deepcopy(separate)
    cast(dict[str, object], shared["first"])["@id"] = "_:shared"
    cast(dict[str, object], shared["second"])["@id"] = "_:shared"
    selectors = [("first", "nodes", "p"), ("second", "nodes", "p")]
    assert compare_context_usages(separate, separate, selectors).verified
    assert compare_context_usages(separate, shared, selectors).outcome == "disagreement"
