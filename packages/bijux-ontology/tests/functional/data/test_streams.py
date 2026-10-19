"""Actual native streaming checks retain local evidence without global claims."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Any
from typing import Literal as ProfileLiteral

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SH

from bijux_ontology.data.streams import check_rdf_stream
from bijux_ontology.data.streams.protocol import read_completion
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.budgets import ControlBudget, Operation
from bijux_ontology.execution.limits import BUDGET_KINDS, Budget, IssueCollector, Limits
from bijux_ontology.execution.processes.ownership import spawn_owned_process
from bijux_ontology.execution.processes.transport import checkpoint, exchange_worker
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.rdf.queries.types import QueryScope
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.stores.preparation import snapshot_rdf
from bijux_ontology.resources.files import verify_files

E = Namespace("urn:stream:")
INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


def _vocabulary() -> Graph:
    """Use only actual modest declarations required by the streamed positions."""
    return (
        Graph()
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.C, RDF.type, OWL.Class))
    )


def test_small_native_oracle_exact_literals_and_independent_global_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "samples.nt"
    raw = (
        f'<urn:stream:s> <urn:stream:p> "01"^^<{INTEGER}> .\n'
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
        f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
        f'<urn:stream:s> <urn:stream:p> "1.5"^^<{INTEGER}> .\n'
    ).encode()
    source.write_bytes(raw)
    descriptor = RdfSource(
        source,
        format="nt",
        document_iri="https://example.test/data",
        effective_url="https://example.test/retrieved",
        role="data",
    )
    result = check_rdf_stream(descriptor, _vocabulary())
    ordinary = check_data_usage(prepare_rdf(descriptor), _vocabulary())
    assert sorted((i.code, i.actual) for i in result.report.issues) == sorted(
        (i.code, i.actual) for i in ordinary.issues
    )
    assert result.parse_report.verified and result.local_report.complete
    assert result.report.outcome == "disagreement" and result.statements == 4
    assert (
        result.bytes_read == len(raw)
        and result.source_sha256 == hashlib.sha256(raw).hexdigest()
    )
    assert (
        result.document_iri == descriptor.document_iri
        and result.effective_url == descriptor.effective_url
    )
    assert [f.statement.index for f in result.findings] == [2, 3, 4]
    assert isinstance(result.findings[-1].statement.value, Literal)
    assert str(result.findings[-1].statement.value) == "1.5"
    source.write_text("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n")
    scoped = check_rdf_stream(
        source, _vocabulary(), required_global=("shapes", "identity", "cardinality")
    )
    assert scoped.parse_report.verified and scoped.local_report.verified
    assert scoped.global_report is not None and not scoped.global_report.verified
    assert not scoped.report.verified
    if importlib.util.find_spec("pyshacl") is not None:
        from bijux_ontology.shacl.checking import validate_shapes

        shape = (
            Graph()
            .add((E.Shape, RDF.type, SH.NodeShape))
            .add((E.Shape, SH.targetNode, E.s))
        )
        shape.add((E.Shape, SH.property, E.Property)).add(
            (E.Property, SH.path, E.required)
        ).add((E.Property, SH.minCount, Literal(1)))
        assert validate_shapes(prepare_rdf(source), shape).conforms is False


def test_large_actual_stream_finds_late_invalid_positions_without_retaining_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.nt"
    digest = hashlib.sha256()
    with source.open("wb") as stream:
        for index in range(20000):
            row = (
                f"<urn:stream:s{index}> <urn:stream:p> <urn:stream:o{index}> .\n"
            ).encode()
            stream.write(row)
            digest.update(row)
        tail = (
            "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
            f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
            f'<urn:stream:s> <urn:stream:p> "fraction"^^<{INTEGER}> .\n'
        ).encode()
        stream.write(tail)
        digest.update(tail)
    limits = Limits(
        max_bytes=2048,
        max_total_bytes=200000000,
        max_work=50000000,
        max_states=2000000,
        max_triples=100000,
    )
    result = check_rdf_stream(source, _vocabulary(), limits=limits)
    assert result.parse_report.verified and result.statements == 20003
    assert (
        result.report.outcome == "disagreement"
        and result.source_sha256 == digest.hexdigest()
    )
    assert len(result.findings) == 3
    assert [row.statement.index for row in result.findings] == [20001, 20002, 20003]
    assert [row.issue.code for row in result.findings] == [
        "vocabulary.term_missing",
        "vocabulary.term_missing",
        "literal.lexical_invalid",
    ]
    assert not hasattr(result, "dataset") and not hasattr(result, "graph")


def test_nquads_exact_graph_roles_and_retained_joint_blank_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "graphs.nq"
    source.write_text(
        "_:shared <urn:stream:missing> _:target _:shared .\n"
        "_:target <urn:stream:missing> _:shared _:shared .\n"
        f'<urn:stream:s> <urn:stream:p> "01"^^<{INTEGER}> <urn:stream:g> .\n'
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(source, _vocabulary())
    assert result.parse_report.verified and len(result.findings) == 3
    first, second, default = (row.statement for row in result.findings)
    assert isinstance(first.graph, BNode) and first.subject is first.graph
    assert second.value is first.graph and second.subject is first.value
    assert default.graph is None
    named = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name=str(E.g)), _vocabulary()
    )
    assert named.report.verified and named.statements == 4 and named.selected == 1
    selected_default = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name="@default"), _vocabulary()
    )
    assert (
        selected_default.report.outcome == "disagreement"
        and selected_default.selected == 1
    )
    assert selected_default.findings[0].statement.graph is None
    absent = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name=str(E.absent)), _vocabulary()
    )
    assert absent.parse_report.verified and not absent.local_report.verified
    assert not any(issue.kind == "finding" for issue in absent.report.issues)
    duplicate = tmp_path / "duplicates.nt"
    duplicate.write_text("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n" * 2)
    checked = check_rdf_stream(duplicate, _vocabulary())
    assert checked.report.verified and checked.statements == 2
    assert (
        check_rdf_stream(
            RdfSource(source, format="nquads", union=True), _vocabulary()
        ).report.verified
        is False
    )


def test_late_parse_issue_limits_and_cancellation_keep_honest_prefix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.nt"
    bad = "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    valid = "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
    source.write_text(bad + "<urn:stream:s> <urn:stream:p> broken .\n")
    partial = check_rdf_stream(source, _vocabulary())
    assert (
        not partial.parse_report.complete
        and partial.findings[0].issue.code == "vocabulary.term_missing"
    )
    failures = [
        i.details.failure
        for i in partial.parse_report.issues
        if i.details is not None and i.details.failure is not None
    ]
    assert (
        failures and failures[0].span is not None and failures[0].span.start_line == 2
    )
    source.write_text(bad * 2)
    capped = check_rdf_stream(source, _vocabulary(), limits=Limits(max_issues=1))
    assert capped.report.outcome == "exhausted" and not capped.parse_report.complete
    assert len(capped.findings) == 1 and any(
        i.code == "vocabulary.term_missing" for i in capped.report.issues
    )
    source.write_text(bad + valid * 100)
    byte_cap = check_rdf_stream(source, _vocabulary(), max_stream_bytes=1000)
    assert (
        byte_cap.report.outcome == "exhausted"
        and byte_cap.findings[0].statement.index == 1
    )
    source.write_text(bad + '<urn:stream:s> <urn:stream:p> "' + "x" * 2000 + '" .\n')
    record_cap = check_rdf_stream(source, _vocabulary(), limits=Limits(max_bytes=512))
    assert (
        record_cap.report.outcome == "exhausted"
        and record_cap.findings[0].statement.index == 1
    )
    event = Event()
    event.set()
    cancelled = check_rdf_stream(source, _vocabulary(), cancel=event)
    assert cancelled.report.outcome == "exhausted"
    source.write_text(valid)
    assert check_rdf_stream(source, _vocabulary()).report.verified
    assert (
        check_rdf_stream(source, _vocabulary(), timeout=0.001).report.outcome
        == "exhausted"
    )
    huge = RdfSource(source, document_iri="https://example.test/" + "x" * 100000)
    bounded = check_rdf_stream(huge, _vocabulary(), limits=Limits(max_bytes=1000))
    assert bounded.report.outcome == "exhausted" and bounded.document_iri is None
    assert all(len(i.location) < 1000 for i in bounded.report.issues)
    shared = Budget()
    shared.consume("issues", shared.remaining("issues"))
    assert check_rdf_stream(source, _vocabulary(), budget=shared).report.verified


def test_disk_vocabulary_uses_actual_sealed_targeted_declarations_and_scope(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "schema.nq"
    schema.write_text(
        f"<urn:stream:p> <{RDF.type}> <{OWL.DatatypeProperty}> _:schema .\n"
        f"<urn:stream:C> <{RDF.type}> <{OWL.Class}> _:schema .\n"
        f"<{E.Custom}> <{RDF.type}> <{E.Kind}> _:schema .\n"
        f'<{E.NotClass}> <{RDF.type}> "not a named kind" _:schema .\n'
    )
    limits = Limits(max_total_bytes=500000000, max_work=10000000, max_states=1000000)
    stored = snapshot_rdf(
        schema, tmp_path / "schema-snapshot", format="nquads", limits=limits
    )
    assert stored.report.verified and stored.snapshot is not None
    snapshot = stored.snapshot
    pin = snapshot.graph_names[0]
    assert isinstance(pin, BNode)
    source = tmp_path / "data.nt"
    source.write_text(
        f"<urn:stream:s> <{RDF.type}> <urn:stream:C> .\n"
        "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("named", pin), limits=limits
    )
    assert result.report.verified
    foreign = check_rdf_stream(
        source,
        snapshot,
        vocabulary_scope=QueryScope("named", BNode(str(pin))),
        limits=limits,
    )
    assert not foreign.report.verified and not any(
        i.kind == "finding" for i in foreign.report.issues
    )
    wrong_scope = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("default"), limits=limits
    )
    assert wrong_scope.report.outcome == "disagreement"
    assert not check_rdf_stream(source, snapshot, limits=limits).report.verified

    with source.open("a") as stream:
        stream.write(
            "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
            f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
            f'<urn:stream:s> <urn:stream:p> "1.5"^^<{INTEGER}> .\n'
        )
    disk = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("named", pin), limits=limits
    )
    memory = check_rdf_stream(source, _vocabulary(), limits=limits)
    assert disk.parse_report.verified and disk.statements == 5
    assert [
        (f.statement.index, f.issue.code, f.issue.actual) for f in disk.findings
    ] == [(f.statement.index, f.issue.code, f.issue.actual) for f in memory.findings]
    assert [f.statement.index for f in disk.findings] == [3, 4, 5]

    with source.open("a") as stream:
        stream.write(
            f"<{E.s}> <{RDF.type}> <{E.Custom}> .\n" * 2
            + f"<{E.s}> <{RDF.type}> <{E.NotClass}> .\n"
        )
    explicit = (
        _vocabulary()
        .add((E.Custom, RDF.type, E.Kind))
        .add((E.NotClass, RDF.type, Literal("not a named kind")))
    )
    disk = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("named", pin), limits=limits
    )
    memory = check_rdf_stream(source, explicit, limits=limits)
    assert [row.issue.to_dict() for row in disk.findings] == [
        row.issue.to_dict() for row in memory.findings
    ]
    assert [row.statement.index for row in disk.findings] == [3, 4, 5, 6, 7, 8]
    assert [row.issue.code for row in disk.findings[-3:]] == [
        "vocabulary.entity_kind_mismatch",
        "vocabulary.entity_kind_mismatch",
        "vocabulary.term_missing",
    ]
    excluded = check_rdf_stream(
        source,
        snapshot,
        vocabulary_scope=QueryScope("named", pin),
        check_literals=False,
        exclude=tuple(map(str, (E.missing, E.MissingClass, E.Custom, E.NotClass))),
        limits=limits,
    )
    assert excluded.report.verified and excluded.statements == 8


def test_explicit_excluded_predicates_remain_in_checked_scope(tmp_path: Path) -> None:
    """A consumer can inspect the exact terms omitted from a passing local check."""
    source = tmp_path / "excluded.nt"
    source.write_text(
        "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(source, _vocabulary(), exclude=(str(E.missing),))
    assert result.report.verified and result.statements == 2
    assert str(E.missing) in result.local_report.exclusions
    assert {"shapes", "identity", "cardinality"} <= set(result.local_report.exclusions)
    assert not check_rdf_stream(source, _vocabulary()).report.verified


def test_inherited_cancellation_reaps_the_actual_stream_worker_and_recovers(
    tmp_path: Path,
) -> None:
    """One parent Event interrupts its native child; expired admission starts none."""
    source = tmp_path / "cancelled.nt"
    source.write_text("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n")
    cancel = Event()
    parent = Operation(None, Budget(), 30, cancel)
    processes: list[subprocess.Popen[bytes]] = []
    interrupted: list[int] = []
    native_allowances: list[dict[str, int]] = []
    counters_before: dict[str, int] = {}
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: ProfileLiteral["call", "return", "c_call", "c_return", "c_exception"],
        value: object,
    ) -> None:
        """Cancel at real supervision after the installed worker and pipes start."""
        if previous is not None:
            previous(frame, event, value)
        if (
            event == "call"
            and frame.f_code is execute_worker.__code__
            and not native_allowances
        ):
            native_allowances.append(
                json.loads(frame.f_locals["payload"])["allowances"]
            )
            counters_before.update(
                {kind: parent._counts.get(kind, 0) for kind in BUDGET_KINDS}
            )
        if (
            event == "return"
            and frame.f_code is spawn_owned_process.__code__
            and isinstance(value, subprocess.Popen)
            and isinstance(value.args, (list, tuple))
            and "bijux_ontology.data.streams.worker" in value.args
        ):
            processes.append(value)
        if (
            event == "call"
            and frame.f_code is checkpoint.__code__
            and frame.f_back is not None
            and frame.f_back.f_code is exchange_worker.__code__
            and processes
            and not cancel.is_set()
        ):
            process = processes[-1]
            assert process.poll() is None
            interrupted.append(process.pid)
            cancel.set()

    sys.setprofile(observe)
    try:
        stopped = check_rdf_stream(source, _vocabulary(), budget=parent)
        assert stopped.report.outcome == "exhausted" and not stopped.report.complete
        assert len(processes) == 1 and interrupted == [processes[0].pid]
        assert {
            kind: parent._counts.get(kind, 0) - counters_before[kind]
            for kind in BUDGET_KINDS
        } == {
            kind: native_allowances[0][kind] if kind != "issues" else 0
            for kind in BUDGET_KINDS
        }
        terminated = processes[0]
        assert terminated.poll() is not None and terminated.returncode != 0
        if os.name == "posix":
            assert terminated.returncode < 0
            with pytest.raises(ProcessLookupError):
                os.kill(terminated.pid, 0)
        expired = Operation(None, Budget(), 0.001, None)
        Event().wait(0.002)
        assert time.monotonic() >= expired.deadline
        denied = check_rdf_stream(source, _vocabulary(), budget=expired)
        assert denied.report.outcome == "exhausted" and len(processes) == 1
        # The cancelled parent stays cancelled; recovery owns a fresh allowance.
        fresh = check_rdf_stream(source, _vocabulary(), budget=Budget())
        assert fresh.report.verified and fresh.statements == 1 and cancel.is_set()
        assert len(processes) == 2 and processes[-1].pid != terminated.pid
        assert processes[-1].poll() == 0
    finally:
        sys.setprofile(previous)


def test_completed_native_finding_is_paid_once_through_inherited_cancellation(
    tmp_path: Path,
) -> None:
    """The real returned prefix survives stop without renewing native permission."""
    source = tmp_path / "completed.nt"
    source.write_text("<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n")
    base, cancel = Budget(), Event()
    parent = Operation(None, base, 30, cancel)
    native: list[dict[str, Any]] = []
    before: dict[str, int] = {}
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, value: object) -> None:
        if frame.f_code is execute_worker.__code__:
            if event == "call":
                before.update(
                    {kind: base._counts.get(kind, 0) for kind in BUDGET_KINDS}
                )
            elif event == "return" and isinstance(value, bytes):
                native.append(json.loads(value))
                cancel.set()

    sys.setprofile(observe)
    try:
        result = check_rdf_stream(source, _vocabulary(), budget=parent)
    finally:
        sys.setprofile(previous)
    assert native and native[0]["consumed"]["work"] > 0
    assert result.report.outcome == "exhausted" and not result.report.complete
    assert len(result.findings) == 1 and result.findings[0].statement.index == 1
    finding = result.findings[0].issue
    assert finding.code == "vocabulary.term_missing"
    assert base._counts["work"] - before["work"] >= native[0]["consumed"]["work"]
    assert base._counts["issues"] - before["issues"] == 1
    assert base._retained_issues[id(finding)] is finding
    retained = IssueCollector(base)
    retained.retain_completed(result.report)
    assert any(issue is finding for issue in retained)
    assert base._counts["issues"] - before["issues"] == 1


def test_bounded_terminal_adoption_keeps_the_paid_prefix_and_wire_policy(
    tmp_path: Path,
) -> None:
    """A real many-finding reply is funded; stopped adoption preserves occurrences."""
    source = tmp_path / "findings.nt"
    source.write_text("<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n" * 20)
    complete = check_rdf_stream(source, _vocabulary())
    assert complete.parse_report.verified and len(complete.findings) == 20
    base = Budget()
    cut: list[ControlBudget] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code is IssueCollector.append.__code__
            and not cut
        ):
            collector = frame.f_locals["self"]
            if isinstance(collector.budget, ControlBudget):
                cut.append(collector.budget)
                collector.budget.consume("states", collector.budget.remaining("states"))

    sys.setprofile(observe)
    try:
        partial = check_rdf_stream(source, _vocabulary(), budget=base)
    finally:
        sys.setprofile(previous)
    assert len(cut) == 1 and partial.report.outcome == "exhausted"
    assert len(partial.findings) == 1 and partial.findings[0].statement.index == 1
    assert base._counts["issues"] == 1
    assert (
        base._retained_issues[id(partial.findings[0].issue)]
        is partial.findings[0].issue
    )


@pytest.mark.parametrize("failure", ("cancel", "revision"))
def test_stored_native_costs_settle_before_live_identity_admission(
    tmp_path: Path, failure: str
) -> None:
    """Completed costs survive when fresh store identity cannot be established."""
    schema = tmp_path / "schema.nt"
    schema.write_text(f"<urn:stream:p> <{RDF.type}> <{OWL.DatatypeProperty}> .\n")
    prepared = snapshot_rdf(schema, tmp_path / "declarations", format="nt")
    assert prepared.report.verified and prepared.snapshot is not None
    snapshot = prepared.snapshot
    source = tmp_path / "stored.nt"
    source.write_text("<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n")
    base, cancel = Budget(), Event()
    parent = Operation(None, base, 30, cancel)
    native: list[dict[str, Any]] = []
    before: dict[str, int] = {}
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, value: object) -> None:
        if frame.f_code is execute_worker.__code__:
            if event == "call":
                before.update(
                    {kind: base._counts.get(kind, 0) for kind in BUDGET_KINDS}
                )
            elif event == "return" and isinstance(value, bytes):
                native.append(json.loads(value))
                if failure == "cancel":
                    cancel.set()
                else:
                    (snapshot.directory / "unpublished-resource").write_text("changed")

    sys.setprofile(observe)
    try:
        result = check_rdf_stream(
            source, snapshot, vocabulary_scope=QueryScope("default"), budget=parent
        )
    finally:
        sys.setprofile(previous)
    assert native and native[0]["records"]
    assert base._counts["work"] - before["work"] >= native[0]["consumed"]["work"]
    assert not result.report.verified and not result.report.complete
    assert result.findings == () and result.local_report.checked == 0
    assert result.report.outcome == (
        "exhausted" if failure == "cancel" else "unverified"
    )


def test_native_issue_counter_is_qualified_before_its_payment_projection(
    tmp_path: Path,
) -> None:
    """Real receipt controls cannot hide an invalid producer issue bound behind zero."""
    source = tmp_path / "native.nt"
    source.write_text("<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n")
    wire: list[dict[str, Any]] = []
    replies: list[dict[str, Any]] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, value: object) -> None:
        if frame.f_code is execute_worker.__code__:
            if event == "call":
                wire.append(json.loads(frame.f_locals["payload"]))
            elif event == "return" and isinstance(value, bytes):
                replies.append(json.loads(value))

    sys.setprofile(observe)
    try:
        actual = check_rdf_stream(source, _vocabulary())
    finally:
        sys.setprofile(previous)
    assert len(actual.findings) == 1 and len(replies) == len(wire) == 1
    receipt, request = replies[0], wire[0]
    for kind in ("issues", "states"):
        counters = dict(receipt["consumed"])
        counters[kind] = request["allowances"][kind] + 1
        invalid = dict(receipt, consumed=counters)
        with pytest.raises(ResourceError, match="counters"):
            read_completion(
                invalid,
                limits=Limits(),
                allowances=request["allowances"],
                maximum=Limits().max_total_bytes,
                budget=Budget(),
            )
    missing = dict(receipt)
    del missing["consumed"]
    with pytest.raises(ResourceError, match="envelope"):
        read_completion(
            missing,
            limits=Limits(),
            allowances=request["allowances"],
            maximum=Limits().max_total_bytes,
            budget=Budget(),
        )


@pytest.mark.parametrize("prefix", (False, True))
def test_syntax_failure_registers_only_the_actual_paid_diagnostic(
    tmp_path: Path, prefix: bool
) -> None:
    """Report-generated absence/completeness issues are not native paid identities."""
    source = tmp_path / "syntax.nt"
    source.write_text(
        ("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n" if prefix else "")
        + "invalid RDF .\n"
    )
    base = Budget()
    result = check_rdf_stream(source, _vocabulary(), budget=base)
    assert result.parse_report.outcome == "input_error"
    paid = next(
        issue for issue in result.report.issues if issue.code == "input.invalid"
    )
    generated = next(
        issue
        for issue in result.local_report.issues
        if issue.code == ("execution.incomplete" if prefix else "input.empty_scope")
    )
    assert base._counts["issues"] == 1
    assert base._retained_issues.get(id(paid)) is paid
    assert id(generated) not in base._retained_issues
    retained = IssueCollector(base)
    retained.retain_completed(result.report)
    assert any(issue is paid for issue in retained)
    assert all(issue is not generated for issue in retained)
    assert base._counts["issues"] == 1


def test_stored_guard_shrink_settles_typed_prefix_before_unused_native_forfeiture(
    tmp_path: Path,
) -> None:
    """A valid receipt cannot lose paid host work to conservative native forfeiture."""
    schema = tmp_path / "schema.nt"
    schema.write_text(f"<urn:stream:p> <{RDF.type}> <{OWL.DatatypeProperty}> .\n")
    prepared = snapshot_rdf(schema, tmp_path / "declarations", format="nt")
    assert prepared.snapshot is not None and prepared.report.verified
    source = tmp_path / "findings.nt"
    source.write_text("<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n" * 2)
    base = Budget()
    guards = 0
    native: list[dict[str, Any]] = []
    previous = sys.getprofile()

    def observe(frame: FrameType, event: str, value: object) -> None:
        nonlocal guards
        if (
            event == "return"
            and frame.f_code is execute_worker.__code__
            and isinstance(value, bytes)
        ):
            native.append(json.loads(value))
        if (
            event == "return"
            and frame.f_code is verify_files.__code__
            and frame.f_back is not None
            and frame.f_back.f_code is check_rdf_stream.__code__
        ):
            guards += 1
            if guards == 2:
                base.consume("work", base.remaining("work") - 600)

    sys.setprofile(observe)
    try:
        result = check_rdf_stream(
            source,
            prepared.snapshot,
            vocabulary_scope=QueryScope("default"),
            budget=base,
        )
    finally:
        sys.setprofile(previous)
    assert guards == 2 and len(native) == 1 and len(native[0]["records"]) == 2
    assert result.report.outcome == "exhausted" and not result.report.complete
    assert len(result.findings) == 1 and result.findings[0].statement.index == 1
    issue = result.findings[0].issue
    assert base._counts["issues"] == 1 and base._retained_issues.get(id(issue)) is issue
    assert base.remaining("work") == 0
