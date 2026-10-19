"""Public finite answers retain RDF identity independently of query execution."""

import json
from dataclasses import replace
from threading import Event
from typing import cast

from rdflib import BNode, Graph

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.answers import (
    AnswerArtifact,
    AnswerFormat,
    capture_answer,
    compare_answers,
    read_answers,
    write_answers,
)
from bijux_ontology.rdf.queries import QueryScope, query_rdf

_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
_TOKEN = "http://www.w3.org/2001/XMLSchema#token"


def _answer(
    rows: list[dict[str, object]], variables: tuple[str, ...] = ("x", "y")
) -> AnswerArtifact:
    """Use real standard decoding for every supplied finite fixture."""
    result = read_answers(
        json.dumps({"head": {"vars": variables}, "results": {"bindings": rows}}),
        format="json",
    )
    assert result.report.verified, result.report.to_dict()
    return result


def _blank(label: str) -> dict[str, str]:
    """Represent a result-local standard blank label."""
    return {"type": "bnode", "value": label}


def _literal(value: str, **metadata: str) -> dict[str, str]:
    """Keep original lexical metadata in standard result cells."""
    return {"type": "literal", "value": value, **metadata}


def test_lossless_native_formats_preserve_rows_literals_and_global_blanks() -> None:
    """All supported lossless formats preserve duplicate and fully unbound rows."""
    original = _answer(
        [
            {"x": _blank("a"), "y": _literal("hello", **{"xml:lang": "en"})},
            {"x": _blank("a")},
            {"x": _blank("a")},
            {},
            {
                "x": _literal("01", datatype=_INTEGER),
                "y": _literal(" a  b ", datatype=_TOKEN),
            },
        ]
    )
    for format in cast(tuple[AnswerFormat, ...], ("json", "xml", "tsv")):
        encoded = write_answers(original, format=format)
        assert encoded.report.verified and encoded.payload is not None
        restored = read_answers(encoded.payload, format=format)
        assert restored.report.verified and len(restored.rows) == 5
        assert str(restored.rows[4][0]) == "01"
        assert str(restored.rows[4][1]) == " a  b "
        assert restored.rows[0][0] is restored.rows[1][0]
        assert restored.rows[3] == (None, None)
        assert compare_answers(original, restored, semantics="sequence").verified
        assert restored.external_complete is None

    for lexical in ("left\rright", "left\r\nright"):
        control = _answer([{"x": _literal(lexical)}])
        encoded = write_answers(control, format="xml")
        assert encoded.payload is not None and encoded.report.verified
        assert b"&#13;" in encoded.payload
        restored = read_answers(encoded.payload, format="xml")
        assert compare_answers(control, restored).verified
    forbidden = _answer([{"x": _literal("left\x00right")}])
    rejected = write_answers(forbidden, format="xml")
    assert rejected.payload is None and rejected.report.outcome == "unverified"


def test_bags_sets_order_and_shared_blank_identity_are_independent() -> None:
    """Duplicate loss and split blanks cannot hide behind row-local relabeling."""
    original = _answer(
        [{"x": _blank("a"), "y": _literal("v")}, {"x": _blank("a")}, {"x": _blank("a")}]
    )
    renamed = _answer(
        [
            {"x": _blank("b")},
            {"y": _literal("v"), "x": _blank("b")},
            {"x": _blank("b")},
        ],
        ("y", "x"),
    )
    assert compare_answers(original, renamed).verified
    assert not compare_answers(original, renamed, semantics="sequence").verified
    assert not compare_answers(original, renamed, strict_variable_order=True).verified
    short = _answer([{"x": _blank("b")}, {"x": _blank("b"), "y": _literal("v")}])
    assert not compare_answers(original, short).verified
    assert compare_answers(original, short, semantics="set").verified
    split = _answer(
        [{"x": _blank("b"), "y": _literal("v")}, {"x": _blank("b")}, {"x": _blank("c")}]
    )
    assert not compare_answers(original, split).verified
    empty = _answer(
        [
            {"x": _blank("b"), "y": _literal("v")},
            {"x": _blank("b"), "y": _literal("")},
            {"x": _blank("b")},
        ]
    )
    assert not compare_answers(original, empty).verified
    symmetric = _answer([{"x": _blank("a")}, {"x": _blank("b")}])
    symmetric_other = _answer([{"x": _blank("d")}, {"x": _blank("c")}])
    assert compare_answers(symmetric, symmetric_other).verified
    zero = _answer([{}, {}], ())
    zero_short = _answer([{}], ())
    assert not compare_answers(zero, zero_short).verified
    assert compare_answers(zero, zero_short, semantics="set").verified


def test_false_ask_csv_loss_and_external_completeness_are_distinct() -> None:
    """False ASK is a complete answer, while CSV cannot recover RDF identity."""
    false = read_answers('{"head":{},"boolean":false}', format="json")
    assert false.report.verified and false.answer is False
    extended = read_answers(
        '{"head":{},"boolean":false,"metadata":{"label":"outside answers"}}',
        format="json",
    )
    assert extended.report.verified and compare_answers(false, extended).verified
    try:
        AnswerArtifact()
    except InputError:
        pass
    else:
        raise AssertionError("Uninitialized artifacts must not be constructible")
    assert compare_answers(false, false).verified
    assert not compare_answers(false, false, require_external_complete=True).verified
    assert write_answers(false, format="tsv").payload is None
    table = _answer([{}, {"x": _literal("")}, {"x": _literal("01", datatype=_INTEGER)}])
    assert write_answers(table, format="csv").payload is None
    loss = write_answers(table, format="csv", allow_lossy=True)
    assert loss.payload is not None and loss.losses and not loss.report.verified
    assert not read_answers(loss.payload, format="csv").report.verified
    assert not compare_answers(table, read_answers(loss.payload, format="csv")).verified


def test_invalid_truncated_and_unsupported_answers_never_become_empty_agreement() -> (
    None
):
    """Malformed widths, duplicate fields and unsupported RDF terms are visible."""
    payloads = [
        '{"head":{"vars":["x"]},"results":{"bindings":[',
        '{"head":{},"boolean":"false"}',
        '{"head":{"vars":["x","x"]},"results":{"bindings":[]}}',
        '{"head":{"vars":[]},"results":{"bindings":[{"x":{"type":"uri","value":"urn:x"}}]}}',
        '{"head":{"vars":["x"]},"results":{"bindings":[{"x":{"type":"triple","value":"x"}}]}}',
        '{"head":{},"boolean":true,"boolean":false}',
    ]
    for payload in payloads:
        broken = read_answers(payload, format="json")
        assert not broken.report.verified and broken.kind is None
        assert not compare_answers(broken, broken).verified
    for xml in (
        '<sparql xmlns="http://www.w3.org/2005/sparql-results#"><head/><results>',
        '<!DOCTYPE sparql [<!ENTITY x "expanded">]><sparql/>',
        '<sparql xmlns="http://www.w3.org/2005/sparql-results#"><head>'
        '<link href="urn:source"/><variable name="x"/></head><results/></sparql>',
    ):
        assert not read_answers(xml, format="xml").report.verified


def test_value_mode_requires_actual_complete_correspondence() -> None:
    """Existing value semantics do not alter exact RDF lexical identity."""
    left = _answer([{"x": _literal("01", datatype=_INTEGER)}])
    right = _answer([{"x": _literal("1", datatype=_INTEGER)}])
    assert not compare_answers(left, right).verified
    assert compare_answers(
        left, right, semantics="sequence", regime="sparql-1.1"
    ).verified
    assert not compare_answers(left, right, regime="sparql-1.1").verified
    assert compare_answers(
        left, right, regime="owl2-datatype-map", row_pairs=((0, 0),)
    ).verified
    a = _answer([{"x": _blank("a")}])
    b = _answer([{"x": _blank("b")}])
    assert not compare_answers(a, b, semantics="sequence", regime="sparql-1.1").verified
    malformed = compare_answers(
        a,
        b,
        semantics="sequence",
        regime="sparql-1.1",
        blank_pairs=cast(tuple[tuple[BNode, BNode], ...], ((),)),
    )
    assert malformed.outcome == "input_error"
    blank_a, blank_b = cast(BNode, a.rows[0][0]), cast(BNode, b.rows[0][0])
    assert compare_answers(
        a,
        b,
        semantics="sequence",
        regime="sparql-1.1",
        blank_pairs=((blank_a, blank_b),),
    ).verified
    assert not compare_answers(
        a,
        b,
        semantics="sequence",
        regime="sparql-1.1",
        blank_pairs=((BNode(str(blank_a)), blank_b),),
    ).verified
    unknown = _answer([{"x": _literal("opaque", datatype="urn:custom:datatype")}])
    assert not compare_answers(
        unknown, unknown, semantics="sequence", regime="sparql-1.1"
    ).verified


def test_shared_budgets_cancel_and_query_truncation_remain_incomplete() -> None:
    """A retained prefix or exhausted parser cannot manufacture a full answer."""
    limits = Limits(max_total_bytes=64)
    budget = Budget(limits)
    stopped = read_answers("x" * 500_000, format="json", budget=budget)
    assert (
        stopped.report.outcome == "exhausted" and budget._counts.get("bytes", 0) <= 64
    )
    event = Event()
    event.set()
    assert (
        read_answers(
            '{"head":{},"boolean":false}', format="json", cancel=event
        ).report.outcome
        == "exhausted"
    )
    assert (
        read_answers(
            '{"head":{},"boolean":false}', format="json", timeout=1e-9
        ).report.outcome
        == "exhausted"
    )
    query = query_rdf(
        Graph(),
        "SELECT ?x WHERE { VALUES ?x { 1 2 } }",
        scope=QueryScope("default"),
        max_rows=1,
    )
    assert not query.report.complete
    partial = capture_answer(query)
    assert (
        not partial.report.verified and not compare_answers(partial, partial).verified
    )
    complete = query_rdf(Graph(), "ASK {}", scope=QueryScope("default"))
    captured = capture_answer(complete)
    assert captured.report.verified and captured.answer is True
    assert compare_answers(captured, captured, require_external_complete=True).verified
    a = _answer([{"x": _literal("one")}])
    b = _answer([{"x": _literal("two")}])
    baseline = Budget()
    assert not compare_answers(a, b, budget=baseline).verified
    cut = Budget(
        replace(baseline.limits, max_work=max(1, baseline._counts["work"] - 1))
    )
    bounded = compare_answers(a, b, budget=cut)
    assert not bounded.verified and cut._counts.get("work", 0) <= cut.limits.max_work
