"""Real maintained expansion, exact inverse boundaries and atomic typed batches."""

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import FrozenInstanceError
from typing import cast, overload

import pytest

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Limits
from bijux_ontology.vocabulary.templates import IdentifierTemplate, TemplateKey


def test_two_key_reserved_unicode_roundtrip_and_owned_order() -> None:
    """Simple expansion escapes delimiters and recognition preserves typed values."""
    keys = [TemplateKey("accession"), TemplateKey("collection")]
    profile = IdentifierTemplate(
        "https://example.org/id/{collection}/{accession}", keys
    )
    keys.clear()
    row: dict[str, str | int] = {"collection": "lab/α", "accession": "A?B#C%"}
    record = profile.construct(row)
    assert record.iri == "https://example.org/id/lab%2F%CE%B1/A%3FB%23C%25"
    row["accession"] = "changed"
    recognized = profile.recognize(record.iri)
    assert recognized.report.verified
    assert recognized.record == record
    assert record.bindings == (("accession", "A?B#C%"), ("collection", "lab/α"))
    with pytest.raises(FrozenInstanceError):
        profile.template = "https://example.org/changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("token", "value", "expected"),
    [
        ("{key}", "Hello World!", "Hello%20World%21"),
        ("{+key}", "/foo/bar", "/foo/bar"),
        ("{key}", "/foo/bar", "%2Ffoo%2Fbar"),
        ("{+key}", "Hello World!", "Hello%20World!"),
    ],
)
def test_maintained_rfc_scalar_examples(token: str, value: str, expected: str) -> None:
    """RFC 6570 simple and reserved expansion examples use the real processor."""
    profile = IdentifierTemplate("https://example.org/" + token, (TemplateKey("key"),))
    assert profile.construct({"key": value}).iri == "https://example.org/" + expected


def test_single_decode_and_exact_iri_image() -> None:
    """Percent escapes are decoded once without normalization or guessed identities."""
    profile = IdentifierTemplate("https://example.org/id/{key}", (TemplateKey("key"),))
    record = profile.construct({"key": "%2F"})
    assert record.iri.endswith("%252F")
    assert profile.recognize(record.iri).record == record
    for iri in (
        "https://example.org/id/%2f",
        "https://example.org/id/%41",
        "https://example.org/id/%FF",
        "https://example.org/id/α",
        "https://example.org/other/value",
    ):
        result = profile.recognize(iri)
        assert result.original_iri == iri
        assert result.record is None
        assert result.report.outcome == "disagreement"
    assert profile.recognize("relative/id").report.outcome == "input_error"
    assert profile.recognize("https://example.org/id/%ZZ").record is None
    assert profile.recognize("https://example.org/id/").record == profile.construct(
        {"key": ""}
    )


def test_integer_bindings_are_canonical_and_strings_preserve_zeroes() -> None:
    """Integers exclude bool/coercion; leading zeroes remain valid string keys."""
    integer = IdentifierTemplate(
        "https://example.org/{key}", (TemplateKey("key", "integer"),)
    )
    for value in (0, -3, 78):
        record = integer.construct({"key": value})
        assert integer.recognize(record.iri).record == record
    for lexical in ("01", "+1", "-0", "1.0", "１２"):
        assert integer.recognize("https://example.org/" + lexical).record is None
    for invalid_value in (True, "1", 1.5, None):
        with pytest.raises(InputError):
            integer.construct(cast(Mapping[str, str | int], {"key": invalid_value}))
    string = IdentifierTemplate("https://example.org/{key}", (TemplateKey("key"),))
    assert string.recognize("https://example.org/001").record == string.construct(
        {"key": "001"}
    )


@pytest.mark.parametrize(
    "template",
    [
        "https://example.org/{+key}",
        "https://example.org/{key:2}",
        "https://example.org/{?key}",
        "https://example.org/{key*}",
        "https://example.org/{key}/{key}",
    ],
)
def test_unsupported_inverse_never_returns_a_record(template: str) -> None:
    """Maintained-valid operators outside the finite inverse remain unknown."""
    profile = IdentifierTemplate(template, (TemplateKey("key"),))
    result = profile.recognize("https://example.org/value")
    assert result.record is None
    assert result.report.outcome == "unverified"
    if template != "https://example.org/{+key}":
        with pytest.raises(InputError, match="Unsupported forward"):
            profile.construct({"key": "value"})


@pytest.mark.parametrize(
    "template",
    ["https://example.org/{left}-{right}", "https://example.org/{left}{right}"],
)
def test_ambiguous_inverse_never_guesses(template: str) -> None:
    """Unreserved separators and adjacent variables cannot identify original keys."""
    profile = IdentifierTemplate(template, (TemplateKey("left"), TemplateKey("right")))
    record = profile.construct({"left": "a-b", "right": "c"})
    assert profile.recognize(record.iri).report.outcome == "unverified"


def test_collisions_and_duplicate_rows_reject_entire_batch() -> None:
    """Distinct records and identical inputs are never silently merged."""
    profile = IdentifierTemplate(
        "https://example.org/{left}-{right}",
        (TemplateKey("left"), TemplateKey("right")),
    )
    rows: list[Mapping[str, str | int]] = [
        {"left": "a-b", "right": "c"},
        {"left": "a", "right": "b-c"},
    ]
    for selected in (rows, [rows[0], rows[0]]):
        result = profile.construct_many(selected)
        assert result.records is None
        assert result.report.outcome == "disagreement"
        assert result.report.checked == 2
        assert result.report.issues[0].evidence == {"first": 0, "second": 1}
    good = profile.construct_many([rows[0], {"left": "other", "right": "value"}])
    assert good.report.verified
    assert good.records is not None and len(good.records) == 2
    assert profile.construct_many([]).records == ()
    invalid = profile.construct_many([rows[0], {"left": "missing"}])
    assert invalid.records is None and invalid.report.outcome == "input_error"
    assert invalid.report.checked == 1


class _IndexedRows(Sequence[Mapping[str, str | int]]):
    """An input that permits bounded indexing but rejects unbounded iteration."""

    def __init__(self, count: int) -> None:
        self.row_count = count
        self.reads: list[int] = []

    def __len__(self) -> int:
        return self.row_count

    @overload
    def __getitem__(self, index: int) -> Mapping[str, str | int]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, str | int]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, str | int] | Sequence[Mapping[str, str | int]]:
        if not isinstance(index, int):
            raise AssertionError("No slicing")
        self.reads.append(index)
        return {"key": str(index)}

    def __iter__(self) -> Iterator[Mapping[str, str | int]]:
        raise AssertionError("No unbounded iteration")


def test_indexed_capture_and_cumulative_limits_preserve_findings() -> None:
    """Limits apply across a batch and stopped work cannot return partial records."""
    profile = IdentifierTemplate("https://example.org/{key}", (TemplateKey("key"),))
    rows = _IndexedRows(3)
    assert profile.construct_many(rows).report.verified
    assert rows.reads == [0, 1, 2]
    too_many = _IndexedRows(4)
    assert (
        profile.construct_many(too_many, limits=Limits(max_states=3)).report.outcome
        == "exhausted"
    )
    assert not too_many.reads
    duplicate: list[Mapping[str, str | int]] = [{"key": "same"}] * 3
    limited = profile.construct_many(duplicate, limits=Limits(max_issues=1))
    assert limited.records is None
    assert [issue.kind for issue in limited.report.issues] == ["finding", "exhausted"]
    limited_bytes = profile.construct_many(rows, limits=Limits(max_total_bytes=70))
    assert limited_bytes.records is None
    assert limited_bytes.report.outcome == "exhausted"
    assert limited_bytes.report.checked < 3
    assert (
        profile.recognize(
            "https://example.org/one", limits=Limits(max_work=1)
        ).report.outcome
        == "exhausted"
    )
    with pytest.raises(LimitExceeded):
        profile.construct({"key": "large"}, limits=Limits(max_bytes=3))


@pytest.mark.parametrize(
    "template",
    [
        "https://example.org/{",
        "https://example.org/{}",
        "https://example.org/{{key}}",
        "https://example.org/{=key}",
    ],
)
def test_malformed_templates_are_input_errors(template: str) -> None:
    """The maintained strict constructor validates syntax behind the bounded guard."""
    with pytest.raises(InputError):
        IdentifierTemplate(template, (TemplateKey("key"),))


def test_missing_extra_and_undeclared_keys_are_rejected() -> None:
    """No missing-value defaults or undeclared-key guesses can enter construction."""
    profile = IdentifierTemplate("https://example.org/{key}", (TemplateKey("key"),))
    for row in (
        {},
        {"other": "x"},
        {"key": "x", "extra": "y"},
        defaultdict(lambda: "invented", {"other": "x"}),
    ):
        with pytest.raises(InputError):
            profile.construct(row)
    with pytest.raises(InputError):
        IdentifierTemplate("https://example.org/{other}", (TemplateKey("key"),))
    with pytest.raises(InputError):
        IdentifierTemplate(
            "https://example.org/{key}", (TemplateKey("key"), TemplateKey("key"))
        )


def test_key_capture_uses_bounded_indexing() -> None:
    """Template metadata capture never follows a custom unbounded iterator."""

    class IndexedKeys(list[TemplateKey]):
        def __iter__(self) -> Iterator[TemplateKey]:
            raise AssertionError("No unbounded iteration")

    keys = IndexedKeys([TemplateKey("key")])
    profile = IdentifierTemplate("https://example.org/{key}", keys)
    assert profile.construct({"key": "value"}).bindings == (("key", "value"),)
    with pytest.raises(LimitExceeded):
        IdentifierTemplate(
            "https://example.org/{key}", keys * 2, limits=Limits(max_states=1)
        )


def test_recognition_rejects_non_string_runtime_inputs() -> None:
    """Invalid objects cannot escape inside an immutable recognition result."""
    profile = IdentifierTemplate("https://example.org/{key}", (TemplateKey("key"),))
    invalid_values: tuple[object, ...] = (
        None,
        1,
        True,
        b"https://example.org/value",
        [],
        {},
    )
    for invalid in invalid_values:
        with pytest.raises(InputError, match="IRI string"):
            profile.recognize(cast(str, invalid))
    relative = profile.recognize("relative/id")
    assert relative.original_iri == "relative/id"
    assert relative.report.outcome == "input_error"


@pytest.mark.parametrize("token", ["{self}", "{+self}"])
def test_bound_method_key_preserves_names_and_avoids_alias_collisions(
    token: str,
) -> None:
    """A legal caller key cannot collide with Python's bound-method parameter."""
    profile = IdentifierTemplate(
        "https://example.org/" + token + "/{_bijux_self}/{_bijux_self_1}",
        (TemplateKey("self"), TemplateKey("_bijux_self"), TemplateKey("_bijux_self_1")),
    )
    values = {"self": "a/b", "_bijux_self": "literal", "_bijux_self_1": "other"}
    record = profile.construct(values)
    assert record.iri == (
        "https://example.org/"
        + ("a%2Fb" if token == "{self}" else "a/b")
        + "/literal/other"
    )
    assert record.bindings == tuple(values.items())
    recognized = profile.recognize(record.iri)
    if token == "{self}":
        assert recognized.report.verified and recognized.record == record
    else:
        assert recognized.report.outcome == "unverified" and recognized.record is None
    unique = profile.construct_many([values, {**values, "self": "second"}])
    assert unique.report.verified and unique.records is not None
    duplicate = profile.construct_many([values, values])
    assert duplicate.records is None and duplicate.report.outcome == "disagreement"
