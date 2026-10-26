"""Actual SSSOM joins distinguish SKOS entailment from explicit application rules."""

import importlib.util
import json
import sys
import threading
import tracemalloc
from collections.abc import Sequence
from dataclasses import replace
from types import FrameType
from typing import Any, cast

import pytest
from rdflib import BNode, Literal, URIRef
from rdflib.namespace import OWL, SKOS

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.vocabulary.mappings.composition import (
    ComposedMapping,
    MappingCompositionRule,
    compose_mappings,
)
from bijux_ontology.vocabulary.mappings.interchange import export_sssom, read_sssom
from bijux_ontology.vocabulary.mappings.types import MappingRead, MappingSet

LICENSE = "https://creativecommons.org/publicdomain/zero/1.0/"
SET_ID = "urn:example:mapping-set"
HEADER = (
    "# mapping_set_id: urn:example:mapping-set\n"
    f"# license: {LICENSE}\n"
    '# mapping_set_title: "Original authored metadata"\n'
    '# curie_map:\n#   ex: "urn:example:"\n'
    "subject_id\tpredicate_id\tobject_id\tmapping_justification\tconfidence\tcomment\n"
)
EXACT = MappingCompositionRule(
    str(SKOS.exactMatch),
    str(SKOS.exactMatch),
    str(SKOS.exactMatch),
    "skos",
    "https://www.w3.org/TR/skos-reference/",
)


def _row(
    subject: str, target: str, predicate: str = "exactMatch", comment: str = "authored"
) -> str:
    """Preserve real lexical metadata through the maintained producer."""
    return (
        f"ex:{subject}\tskos:{predicate}\tex:{target}\t"
        f"semapv:ManualMappingCuration\t0.900\t{comment}\n"
    )


def _read(body: str, header: str = HEADER) -> MappingSet:
    """Use only the actual public optional interchange factory for positives."""
    if importlib.util.find_spec("sssom") is None:
        pytest.skip("Actual SSSOM fixtures require optional sssom tooling")
    result = read_sssom(
        (header + body).encode(), mapping_set_id=SET_ID, license=LICENSE
    )
    assert result.report.verified, result.report.to_dict()
    assert result.mapping_set is not None
    return result.mapping_set


@pytest.fixture(scope="module")
def pair() -> tuple[MappingSet, MappingSet]:
    """Duplicate source occurrences and conflicting targets remain separate."""
    return (
        _read(
            _row("A", "B", comment="first")
            + _row("A", "B", comment="duplicate")
            + _row("A", "D")
        ),
        _read(_row("B", "C") + _row("B", "E") + _row("D", "F") + _row("Unused", "Z")),
    )


def test_exact_transitivity_retains_all_original_paths_metadata_and_targets(
    pair: tuple[MappingSet, MappingSet],
) -> None:
    left, right = pair
    result = compose_mappings(
        left, right, intermediates=["urn:example:B", "urn:example:D"], rules=[EXACT]
    )
    assert result.report.verified and len(result.results) == len(result.paths) == 5
    assert result.left is left and result.right is right
    assert {row.object for row in result.results} == {
        "urn:example:C",
        "urn:example:E",
        "urn:example:F",
    }
    assert all(
        row.predicate == str(SKOS.exactMatch) and row.rule.basis == "skos"
        for row in result.results
    )
    assert [path.left.index for path in result.paths] == [0, 0, 1, 1, 2]
    assert all(path.left.row is left.rows[path.left.index] for path in result.paths)
    assert all(
        dict(path.left.row.cells)["confidence"] == "0.900" for path in result.paths
    )
    assert (
        len(result.right.rows) == 4
        and "Original authored metadata" in result.left.metadata_json
    )
    assert all(row.predicate != str(OWL.sameAs) for row in result.results)
    application = MappingCompositionRule(
        EXACT.left,
        EXACT.right,
        str(SKOS.closeMatch),
        "application",
        "urn:policy:local-chain",
    )
    repeated = compose_mappings(
        left,
        right,
        intermediates=["urn:example:B", "urn:example:D"],
        rules=[EXACT, application, EXACT],
    )
    assert repeated.report.verified and len(repeated.results) == 15
    assert [row.rule.basis for row in repeated.results[:3]] == [
        "skos",
        "application",
        "skos",
    ]
    assert [row.predicate for row in repeated.results[:3]] == [
        EXACT.result,
        application.result,
        EXACT.result,
    ]
    original = export_sssom(left)
    assert original.report.verified and original.content is not None
    reread = read_sssom(original.content, mapping_set_id=SET_ID, license=LICENSE)
    assert reread.mapping_set is not None and reread.mapping_set.rows == left.rows


def test_broad_close_mixed_and_application_bases_never_gain_standard_transitivity() -> (
    None
):
    left = _read(
        _row("A", "B", "broadMatch") + _row("A", "D", "closeMatch") + _row("A", "F")
    )
    right = _read(
        _row("B", "C", "broadMatch")
        + _row("D", "E", "closeMatch")
        + _row("F", "G", "broadMatch")
    )
    selected = ["urn:example:B", "urn:example:D", "urn:example:F"]
    raw = compose_mappings(left, right, intermediates=selected, rules=[EXACT])
    assert raw.report.verified and not raw.results
    assert [path.status for path in raw.paths] == ["no_rule"] * 3
    application = MappingCompositionRule(
        str(SKOS.broadMatch),
        str(SKOS.broadMatch),
        str(SKOS.broadMatch),
        "application",
        "urn:policy:reviewed-broad-chain",
    )
    applied = compose_mappings(
        left, right, intermediates=selected, rules=[EXACT, application]
    )
    assert applied.report.verified and len(applied.results) == 1
    assert (
        applied.results[0].rule.basis == "application"
        and applied.results[0].rule.identifier == application.identifier
    )
    false_standard = MappingCompositionRule(
        application.left,
        application.right,
        application.result,
        "skos",
        "urn:policy:not-a-skos-axiom",
    )
    rejected = compose_mappings(
        left, right, intermediates=selected, rules=[false_standard]
    )
    assert not rejected.report.verified and not rejected.results
    assert any(
        issue.code == "mapping.composition_rule_unverified" for issue in rejected.report
    )
    identity = MappingCompositionRule(
        EXACT.left, EXACT.right, str(OWL.sameAs), "application", "urn:policy:identity"
    )
    unsupported = compose_mappings(
        left, right, intermediates=selected, rules=[identity]
    )
    assert not unsupported.report.verified and not unsupported.results


def test_uninterpreted_paths_cannot_become_candidates() -> None:
    right = _read(_row("B", "C"))
    typed = _read(
        _row("A", "B").rstrip("\n") + "\towl class\n",
        HEADER.replace("\tcomment\n", "\tcomment\tsubject_type\n"),
    )
    unknown = compose_mappings(
        typed, right, intermediates=["urn:example:B"], rules=[EXACT]
    )
    assert not unknown.report.verified and not unknown.results
    assert unknown.paths[0].status == "unverified" and unknown.paths[
        0
    ].left.row.subject_type == str(OWL.Class)
    modified = _read(
        _row("A", "B").rstrip("\n") + "\tNot\n",
        HEADER.replace("\tcomment\n", "\tcomment\tpredicate_modifier\n"),
    )
    assert not compose_mappings(
        modified, right, intermediates=["urn:example:B"], rules=[EXACT]
    ).results
    row_other = _read(
        _row("A", "B").rstrip("\n") + "\tcustom:qualifier=uninterpreted\n",
        HEADER.replace("\tcomment\n", "\tcomment\tother\n"),
    )
    metadata_other = _read(
        _row("A", "B"),
        HEADER.replace(
            "# curie_map:", "# other: custom:qualifier=uninterpreted\n# curie_map:"
        ),
    )
    for original in (row_other, metadata_other):
        unmodeled = compose_mappings(
            original, right, intermediates=["urn:example:B"], rules=[EXACT]
        )
        assert not unmodeled.report.verified and not unmodeled.results
        assert unmodeled.paths[0].status == "unverified" and unmodeled.left is original
    sentinel = _read(_row("A", "B").replace("ex:A", "sssom:NoTermFound"))
    assert not compose_mappings(
        sentinel, right, intermediates=["urn:example:B"], rules=[EXACT]
    ).report.verified


def test_selection_exactness_dynamic_capture_and_malformed_rules(
    pair: tuple[MappingSet, MappingSet],
) -> None:
    left, right = pair
    wrong = compose_mappings(
        left, right, intermediates=["urn:example:b"], rules=[EXACT]
    )
    assert wrong.report.verified and not wrong.paths and not wrong.results
    uri = URIRef("urn:example:B")
    selected = compose_mappings(
        left, right, intermediates=[uri, str(uri)], rules=[EXACT]
    )
    assert (
        selected.report.verified
        and len(selected.results) == 4
        and selected.intermediates == (str(uri),)
    )
    for invalid in (BNode("urn:example:B"), Literal("urn:example:B")):
        bad = compose_mappings(
            left, right, intermediates=[cast(str, invalid)], rules=[EXACT]
        )
        assert bad.report.outcome == "input_error" and not bad.paths
    assert (
        compose_mappings(left, right, intermediates=[], rules=[EXACT]).report.outcome
        == "input_error"
    )
    assert (
        compose_mappings(left, right, intermediates=[str(uri)], rules=[]).report.outcome
        == "input_error"
    )

    class Once(Sequence[str]):
        def __init__(self) -> None:
            self.reads = 0

        def __len__(self) -> int:
            self.reads += 1
            assert self.reads == 1
            return 1

        def __getitem__(self, index: Any) -> Any:
            assert index == 0
            return uri

    captured = Once()
    assert compose_mappings(
        left, right, intermediates=captured, rules=[EXACT]
    ).report.verified
    assert captured.reads == 1


def test_full_input_join_bounds_cumulative_budget_cancel_and_optional_absence(
    pair: tuple[MappingSet, MappingSet],
) -> None:
    left, right = pair
    selected = ["urn:example:B"]
    giant = MappingCompositionRule(
        EXACT.left,
        EXACT.right,
        EXACT.result,
        "application",
        "urn:policy:" + "x" * 100000,
    )
    bounded = compose_mappings(
        left,
        right,
        intermediates=selected,
        rules=[giant],
        limits=Limits(max_bytes=1000),
    )
    assert (
        bounded.report.outcome == "exhausted"
        and not bounded.rules
        and bounded.left is None
    )
    tiny = compose_mappings(
        left, right, intermediates=selected, rules=[EXACT], limits=Limits(max_states=1)
    )
    assert tiny.report.outcome == "exhausted" and not tiny.paths and not tiny.results
    shared = Budget()
    good = compose_mappings(
        left, right, intermediates=selected, rules=[EXACT], budget=shared
    )
    assert good.report.verified
    states = shared.limits.max_states - shared.remaining("states")
    fanout = compose_mappings(
        left,
        right,
        intermediates=selected,
        rules=[EXACT],
        limits=Limits(max_states=states - 1),
    )
    assert (
        fanout.report.outcome == "exhausted" and not fanout.paths and not fanout.results
    )
    shared.consume("work", shared.remaining("work"))
    assert (
        compose_mappings(
            left, right, intermediates=selected, rules=[EXACT], budget=shared
        ).report.outcome
        == "exhausted"
    )
    cancel = threading.Event()
    cancel.set()
    assert (
        compose_mappings(
            left, right, intermediates=selected, rules=[EXACT], cancel=cancel
        ).report.outcome
        == "exhausted"
    )
    assert compose_mappings(
        left, right, intermediates=selected, rules=[EXACT]
    ).report.verified
    # Observe actual candidate construction without replacing its implementation.
    # A caller thread cancels during the prepaid final result fanout.
    active_cancel, started = threading.Event(), threading.Event()

    def stop() -> None:
        if started.wait(20):
            active_cancel.set()

    def observe(frame: FrameType, event: str, arg: Any) -> None:
        if (
            event == "call"
            and frame.f_code.co_name == "__init__"
            and isinstance(frame.f_locals.get("self"), ComposedMapping)
        ):
            started.set()

    thread = threading.Thread(target=stop)
    thread.start()
    original_profile = sys.getprofile()
    sys.setprofile(observe)
    try:
        interrupted = compose_mappings(
            left,
            right,
            intermediates=["urn:example:D"],
            rules=[EXACT] * 50000,
            limits=Limits(
                max_work=200000000, max_states=2000000, max_total_bytes=500000000
            ),
            cancel=active_cancel,
        )
    finally:
        sys.setprofile(original_profile)
        started.set()
        thread.join(2)
    assert not thread.is_alive() and active_cancel.is_set()
    assert 0 < len(interrupted.results) < 50000
    assert interrupted.report.outcome == "exhausted"
    assert not interrupted.report.complete


def test_raw_optional_interchange_report_remains_required() -> None:
    raw = read_sssom(
        (HEADER + _row("A", "B")).encode(), mapping_set_id=SET_ID, license=LICENSE
    )
    result = compose_mappings(raw, raw, intermediates=["urn:example:B"], rules=[EXACT])
    if importlib.util.find_spec("sssom") is None:
        assert raw.mapping_set is None and not raw.report.verified
        assert not result.report.verified and not result.results
        assert raw.report in result.report.children
    else:
        assert result.report.verified and result.left is raw.mapping_set


@pytest.mark.parametrize("field", ["row", "cell", "prefix", "metadata"])
def test_whole_input_qualifies_even_unmatched_rows_and_context(
    pair: tuple[MappingSet, MappingSet], field: str
) -> None:
    """A valid selected path cannot conceal unrelated invalid owned input fields."""
    left, right = pair
    attribute = (
        "prefixes"
        if field == "prefix"
        else "metadata_json"
        if field == "metadata"
        else "rows"
    )
    original = getattr(left, attribute)
    changed: object
    if field == "row":
        changed = (*left.rows[:-1], replace(left.rows[-1], subject="relative"))
    elif field == "cell":
        changed = (
            *left.rows[:-1],
            replace(left.rows[-1], cells=(("comment", cast(str, None)),)),
        )
    elif field == "prefix":
        changed = (*left.prefixes, ("unused", "relative"))
    else:
        changed = '{"other":1,"other":2}'
    object.__setattr__(left, attribute, changed)
    try:
        result = compose_mappings(
            left, right, intermediates=["urn:example:B"], rules=[EXACT]
        )
    finally:
        object.__setattr__(left, attribute, original)
    assert result.report.outcome == "input_error"
    assert not result.report.complete and not result.paths and not result.results


def test_metadata_decoding_cannot_overspend_its_actual_enclosing_grant(
    pair: tuple[MappingSet, MappingSet],
) -> None:
    """Prepay the maintained decoder before allocating nested metadata containers."""
    left, right = pair
    original = left.metadata_json
    object.__setattr__(left, "metadata_json", json.dumps({"other": ["xxxx"] * 2_000}))
    owner = Budget(Limits(max_total_bytes=90_000))
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        result = compose_mappings(
            left, right, intermediates=["urn:example:B"], rules=[EXACT], budget=owner
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
        object.__setattr__(left, "metadata_json", original)
    assert (
        result.report.outcome == "exhausted" and not result.paths and not result.results
    )
    assert owner._counts["bytes"] <= owner.limits.max_total_bytes
    assert peak - current < owner.limits.max_total_bytes


def test_incomplete_native_producer_cannot_qualify_a_usable_selected_row(
    pair: tuple[MappingSet, MappingSet],
) -> None:
    """Actual failed producer truth survives an apparently usable table reference."""
    left, right = pair
    failed = read_sssom(
        b"subject_id\tpredicate_id\tobject_id\nrelative\tbad\tbad\n",
        mapping_set_id=SET_ID,
        license=LICENSE,
    )
    assert not failed.report.verified and failed.report.issues
    result = compose_mappings(
        MappingRead(left, failed.report),
        right,
        intermediates=["urn:example:B"],
        rules=[EXACT],
    )
    assert not result.report.verified and not result.paths and not result.results
    assert result.left is None and failed.report in result.report.children
    assert all(issue in result.report.issues for issue in failed.report.issues)


def test_unrelated_rules_do_not_multiply_selected_join_storage() -> None:
    """Rule admission grows linearly, independently of the selected path product."""
    left = _read(_row("A", "B") * 8)
    right = _read(_row("B", "C") * 8)
    unrelated = [
        MappingCompositionRule(
            f"urn:left:{index}",
            f"urn:right:{index}",
            "urn:result",
            "application",
            f"urn:rule:{index}",
        )
        for index in range(100)
    ]
    budgets = [Budget(), Budget()]
    results = [
        compose_mappings(
            left, right, intermediates=["urn:example:B"], rules=rules, budget=owner
        )
        for rules, owner in zip(([EXACT], [EXACT, *unrelated]), budgets, strict=True)
    ]
    assert all(result.report.verified for result in results)
    assert results[0].paths == results[1].paths
    assert results[0].results == results[1].results
    extra_states = budgets[1]._counts["states"] - budgets[0]._counts["states"]
    assert extra_states < 100 * 20
    # The same real many-to-many expansion is refused before retaining any path.
    limited = compose_mappings(
        left,
        right,
        intermediates=["urn:example:B"],
        rules=[EXACT],
        limits=Limits(max_states=budgets[0]._counts["states"] - 1),
    )
    assert limited.report.outcome == "exhausted"
    assert not limited.paths and not limited.results
