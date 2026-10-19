"""Real JSON-LD lists retain boundaries, duplicate members and exact RDF terms."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.jsonld.checking import check_context, prepare_usages
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.lists import ExpectedList
from bijux_ontology.limits import Limits

P = "https://lists.example/items"
XSD = "http://www.w3.org/2001/XMLSchema#"


def _document(*groups: Sequence[object]) -> dict[str, object]:
    """Supply actual independent lists to the maintained JSON-LD processor."""
    return {
        "@context": {"items": P},
        "items": [{"@list": list(group)} for group in groups],
    }


def test_outer_lists_are_unordered_but_boundaries_and_member_order_are_exact() -> None:
    expected = ContextExpectation(
        ("items",),
        P,
        lists=(
            ExpectedList((1, 2)),
            ExpectedList((3, 4)),
        ),
    )
    for groups in (([1, 2], [3, 4]), ([3, 4], [1, 2])):
        assert check_context(_document(*groups), (expected,)).verified
    regrouped = check_context(_document([1, 2, 3, 4]), (expected,))
    assert [issue.code for issue in regrouped.issues] == ["context.list_group_mismatch"]
    reordered = check_context(_document([2, 1], [3, 4]), (expected,))
    assert [issue.code for issue in reordered.issues] == ["context.list_order_mismatch"]
    plain = check_context(
        {"items": [1, 2, 3, 4], "@context": {"items": P}}, (expected,)
    )
    assert [issue.code for issue in plain.issues] == ["context.list_group_mismatch"]


def test_named_nested_references_duplicates_empty_lists_and_legacy_single_list() -> (
    None
):
    reference = {"@id": "urn:child", "urn:name": "Child"}
    document = _document([reference, reference, "end"], [])
    before = deepcopy(document)
    expected = ContextExpectation(
        ("items",),
        P,
        lists=(
            ExpectedList(
                ({"@id": "urn:child"}, {"@id": "urn:child"}, {"@value": "end"})
            ),
            ExpectedList(()),
        ),
    )
    prepared = prepare_usages(document)
    assert prepared.check((expected,)).verified
    assert document == before
    changed = _document([reference, "end"], [])
    assert not check_context(changed, (expected,)).verified
    assert not check_context(
        _document([{"@id": "urn:other"}, reference, "end"], []), (expected,)
    ).verified
    legacy = ContextExpectation(
        ("items", 0),
        P,
        ordered_values=(
            {"@id": "urn:child", "urn:name": [{"@value": "Child"}]},
            {"@id": "urn:child", "urn:name": [{"@value": "Child"}]},
            {"@value": "end"},
        ),
    )
    assert prepared.check((legacy,)).verified
    flat = ContextExpectation(("items",), P, ordered_values=({"@value": "end"},))
    assert prepared.check((flat,)).outcome == "unverified"
    assert check_context(
        _document([]), (ContextExpectation(("items",), P, lists=(ExpectedList(()),)),)
    ).verified


@pytest.mark.parametrize("legacy", (False, True))
def test_exact_literal_terms_do_not_use_python_numeric_or_lexical_equality(
    legacy: bool,
) -> None:
    def expectation(values: tuple[object, ...]) -> ContextExpectation:
        return (
            ContextExpectation(("items",), P, ordered_values=values)
            if legacy
            else ContextExpectation(("items",), P, lists=(ExpectedList(values),))
        )

    integer = {"@value": "01", "@type": XSD + "integer"}
    expected = expectation((integer,))
    assert check_context(_document([integer]), (expected,)).verified
    for changed in (
        {"@value": "1", "@type": XSD + "integer"},
        {"@value": "01", "@type": XSD + "string"},
        True,
    ):
        report = check_context(_document([changed]), (expected,))
        assert report.outcome == "disagreement"
    assert (
        check_context(
            _document([True]),
            (expectation(({"@value": 1},)),),
        ).outcome
        == "disagreement"
    )
    assert (
        check_context(_document([False]), (expectation(({"@value": 0.0},)),)).outcome
        == "disagreement"
    )
    assert check_context(
        _document(["word"]),
        (expectation(({"@value": "word", "@type": XSD + "string"},)),),
    ).verified
    language = expectation(({"@value": "word", "@language": "EN"},))
    assert check_context(
        _document([{"@value": "word", "@language": "en"}]), (language,)
    ).verified


def test_expectations_own_payloads_ingest_json_and_respect_operation_bounds() -> None:
    value = {"@value": "value"}
    expected_list = ExpectedList((value,))
    value["@value"] = "changed"
    exported = cast(dict[str, object], expected_list.values[0])
    exported["@value"] = "changed again"
    assert expected_list.values == ({"@value": "value"},)
    with pytest.raises(FrozenInstanceError):
        expected_list._payload = "[]"  # type: ignore[misc]
    parsed = expectations_from_data(
        [
            {
                "path": ["items"],
                "predicate": P,
                "lists": [[1, 2], [3, 4]],
            }
        ]
    )
    assert check_context(_document([3, 4], [1, 2]), parsed).verified
    with pytest.raises(InputError, match="Choose lists or ordered_values"):
        ContextExpectation(("items",), P, lists=(expected_list,), ordered_values=())
    invalid: Any = [expected_list]
    with pytest.raises(InputError, match="tuple of ExpectedList"):
        ContextExpectation(("items",), P, lists=invalid)
    huge = ContextExpectation(("items",), P, lists=(ExpectedList(("x" * 1000,)),))
    bounded = check_context(_document(["small"]), (huge,), limits=Limits(max_bytes=500))
    assert bounded.outcome == "exhausted" and not bounded.verified
    unknown = check_context(
        _document([{"urn:name": "anonymous"}]),
        (
            ContextExpectation(
                ("items",), P, lists=(ExpectedList(({"urn:name": "anonymous"},)),)
            ),
        ),
    )
    assert unknown.outcome == "unverified"
    assert "context.list_unverified" in {issue.code for issue in unknown.issues}
