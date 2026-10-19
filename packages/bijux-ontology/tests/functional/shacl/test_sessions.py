"""Persistent SHACL contracts preserve standalone truth and recover owned workers."""

import json
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology import InputError, prepare_source
from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.shacl import ShaclCoverageContract, validate_shapes
from bijux_ontology.shacl.session import ShaclSession

EX = Namespace("https://session.example/")


def _shapes() -> Graph:
    """Require one integer value on each explicitly authored record target."""
    return (
        Graph()
        .add((EX.Shape, RDF.type, SH.NodeShape))
        .add((EX.Shape, SH.targetClass, EX.Record))
        .add((EX.Shape, SH.property, EX.Property))
        .add((EX.Property, SH.path, EX.value))
        .add((EX.Property, SH.minCount, Literal(1)))
        .add((EX.Property, SH.datatype, XSD.integer))
    )


def _data(value: int | str = 1) -> Graph:
    return (
        Graph()
        .add((EX.one, RDF.type, EX.Record))
        .add((EX.one, EX.value, Literal(value)))
    )


def _slow_shapes() -> Graph:
    """Use an actual bounded trusted SPARQL workload, never a Python callback."""
    graph = _shapes()
    graph.add((EX.Shape, SH.sparql, EX.Constraint))
    graph.add(
        (
            EX.Constraint,
            SH.select,
            Literal(
                f"SELECT $this WHERE {{ $this <{EX.slow}> true . "
                f"?a <{EX.number}> ?x . ?b <{EX.number}> ?y . ?c <{EX.number}> ?z . "
                "FILTER(?x + ?y + ?z < 0) }"
            ),
        )
    )
    return graph


def _slow_data() -> Graph:
    graph = _data().add((EX.one, EX.slow, Literal(True)))
    for index in range(100):
        graph.add((EX[str(index)], EX.number, Literal(index)))
    return graph


def test_prepared_shapes_reuse_one_worker_and_match_standalone_raw_truth() -> None:
    shapes, ontology = _shapes(), Graph().add((EX.Marker, RDF.type, EX.Metadata))
    datasets = (_data(), _data("wrong"), _data(2))
    standalone = tuple(
        validate_shapes(data, shapes, ontology=ontology) for data in datasets
    )
    with ShaclSession(
        shapes,
        ontology=ontology,
        preflight=True,
        coverage=ShaclCoverageContract(minimum_focus=1),
    ) as session:
        shapes.remove((None, None, None))
        ontology.remove((None, None, None))
        batch = session.validate_batch(datasets)
        assert session.worker_launches == 1
        assert tuple(item.conforms for item in batch.results) == (True, False, True)
        assert batch.report.complete and not batch.report.verified
        assert len(batch.report.issues) == 1
        for expected, actual in zip(standalone, batch.results, strict=True):
            assert (
                actual.conformance_report.verified
                == expected.conformance_report.verified
            )
            assert tuple(
                finding.constraint_component for finding in actual.findings
            ) == tuple(finding.constraint_component for finding in expected.findings)
            assert actual.coverage is not None and actual.coverage.report.verified
        exported = batch.results[1].report_graph()
        exported.remove((None, None, None))
        assert len(batch.results[1].report_graph()) > 0
    session.close()
    with pytest.raises(InputError, match="closed"):
        session.validate(_data())


def test_prepared_anonymous_shape_and_shared_blank_targets_keep_exact_identity() -> (
    None
):
    blank = BNode()
    data = prepare_source(Graph().add((blank, EX.value, Literal(1))))
    owned = next(data.graph().subjects(EX.value))
    assert isinstance(owned, BNode)
    root = BNode()
    shapes = prepare_source(
        Graph()
        .add((root, RDF.type, SH.NodeShape))
        .add((root, SH.nodeKind, SH.BlankNode))
    )
    shape = next(shapes.graph().subjects(RDF.type, SH.NodeShape))
    assert isinstance(shape, BNode)
    with ShaclSession(shapes, use_shapes=(shape,), mode="pairs") as session:
        assert session.validate(data, focus_nodes=(owned,)).report.verified
        foreign = session.validate(data, focus_nodes=(BNode(str(owned)),))
        assert foreign.report.outcome == "input_error"
        assert session.validate(data, focus_nodes=(owned,)).report.verified
        assert session.worker_launches == 1
    literal_shapes = prepare_source(
        Graph()
        .add((root, RDF.type, SH.NodeShape))
        .add((root, SH.datatype, XSD.integer))
    )
    selected = next(literal_shapes.graph().subjects(RDF.type, SH.NodeShape))
    assert isinstance(selected, BNode)
    with ShaclSession(literal_shapes, use_shapes=(selected,), mode="pairs") as session:
        assert session.validate(
            prepare_source(Graph()),
            focus_nodes=(Literal("01", datatype=XSD.integer, normalize=False),),
        ).report.verified
        assert (
            session.validate(
                prepare_source(Graph()), focus_nodes=(Literal("bad"),)
            ).conforms
            is False
        )
    shared = BNode("same")
    shape_graph = (
        Graph()
        .add((EX.Shape, RDF.type, SH.NodeShape))
        .add((EX.Shape, SH.targetNode, shared))
        .add((EX.Shape, SH.property, EX.Property))
        .add((EX.Property, SH.path, EX.value))
        .add((EX.Property, SH.minCount, Literal(1)))
    )
    with ShaclSession(shape_graph) as session:
        assert session.validate(
            Graph().add((shared, EX.value, Literal(1)))
        ).report.verified
        assert (
            session.validate(
                Graph().add((BNode("same"), EX.value, Literal(1)))
            ).conforms
            is False
        )
        assert session.worker_launches == 1


def test_record_local_conformance_cannot_prove_cross_record_uniqueness() -> None:
    shapes = (
        Graph()
        .add((EX.Shape, RDF.type, SH.NodeShape))
        .add((EX.Shape, SH.targetClass, EX.Record))
        .add((EX.Shape, SH.sparql, EX.Unique))
        .add(
            (
                EX.Unique,
                SH.select,
                Literal(
                    f"SELECT $this WHERE {{ $this <{EX.key}> ?key . "
                    f"?other <{EX.key}> ?key . FILTER(?other != $this) }}"
                ),
            )
        )
    )
    records = tuple(
        Graph()
        .add((node, RDF.type, EX.Record))
        .add((node, EX.key, Literal("duplicate")))
        for node in (EX.one, EX.two)
    )
    with ShaclSession(shapes, trusted_sparql=True) as session:
        batch = session.validate_batch(records, scope="records")
        assert all(
            result.conforms is True and result.conformance_report.verified
            for result in batch.results
        )
        assert not batch.report.verified and not batch.report.complete
        assert "shacl.dataset_scope_unverified" in {
            issue.code for issue in batch.report.issues
        }
        assert session.validate(records[0] + records[1]).conforms is False
        assert session.worker_launches == 1


def test_cancelled_waiter_does_not_stop_active_job_and_cancelled_worker_recovers() -> (
    None
):
    with ShaclSession(_slow_shapes(), trusted_sparql=True, timeout=5) as session:
        assert session.validate(_data()).report.verified
        active_cancel = threading.Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(session.validate, _slow_data(), cancel=active_cancel)
            time.sleep(0.1)
            waiting_cancel = threading.Event()
            waiting_cancel.set()
            waiting = session.validate(_data(), cancel=waiting_cancel)
            assert waiting.report.outcome == "exhausted"
            assert not active.done()
            active_cancel.set()
            stopped = active.result(timeout=3)
        assert stopped.report.outcome == "exhausted" and stopped.conforms is None
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 2


def test_deadline_and_crashed_worker_restart_without_mixed_batch_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ShaclSession(_slow_shapes(), trusted_sparql=True, timeout=30) as session:
        assert session.validate(_data()).report.verified
        started = time.monotonic()
        with monkeypatch.context() as deadline:
            deadline.setattr(session, "_timeout", 1)
            stopped = session.validate(_slow_data())
        assert time.monotonic() - started < 3
        assert stopped.report.outcome == "exhausted" and not stopped.report.complete
        assert session.validate(_data()).report.verified
        process = session._backend._process
        assert process is not None
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=2)
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 3


def test_malformed_wire_request_and_response_discard_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ShaclSession(_shapes()) as session:
        assert session.validate(_data()).report.verified
        exchange = session._backend._exchange
        sent = False

        def malformed(
            payload: bytes,
            deadline: float | None,
            cancel: threading.Event | None,
            *,
            budget: Budget | None = None,
        ) -> bytes:
            nonlocal sent
            if not sent and json.loads(payload).get("command") == "validate":
                sent = True
                payload = b'{"command":"unrecognized"}'
            return exchange(payload, deadline, cancel, budget=budget)

        monkeypatch.setattr(session._backend, "_exchange", malformed)
        invalid = session.validate(_data())
        assert invalid.report.outcome == "input_error" and invalid.conforms is None
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 2
        monkeypatch.setattr(
            session._backend, "_exchange", lambda *args, **kwargs: b'{"bad":'
        )
        invalid_response = session.validate(_data())
        assert invalid_response.report.outcome == "input_error"
        monkeypatch.setattr(session._backend, "_exchange", exchange)
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 3


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("conforms", "false"),
        ("conforms", False),
        ("conforms", None),
        ("report", ""),
        ("report", None),
        ("warnings", {}),
    ),
)
def test_malformed_result_truth_restarts_before_the_next_real_dataset(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ShaclSession(_shapes()) as session:
        exchange = session._backend._exchange
        changed = False

        def malformed(
            payload: bytes,
            deadline: float | None,
            cancel: threading.Event | None,
            *,
            budget: Budget | None = None,
        ) -> bytes:
            nonlocal changed
            raw = exchange(payload, deadline, cancel, budget=budget)
            if not changed and json.loads(payload).get("command") == "validate":
                changed = True
                response = json.loads(raw)
                response[field] = value
                return json.dumps(response).encode()
            return raw

        monkeypatch.setattr(session._backend, "_exchange", malformed)
        batch = session.validate_batch((_data(), _data()))
        assert batch.results[0].conforms is None
        assert batch.results[0].report.outcome == "input_error"
        assert not batch.report.verified and not batch.report.complete
        assert batch.results[1].report.verified
        assert session.worker_launches == 2


def test_preparation_cannot_substitute_a_prior_dataset_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with ShaclSession(_shapes()) as session:
        exchange = session._backend._exchange
        previous: bytes | None = None

        def capture(
            payload: bytes,
            deadline: float | None,
            cancel: threading.Event | None,
            *,
            budget: Budget | None = None,
        ) -> bytes:
            nonlocal previous
            raw = exchange(payload, deadline, cancel, budget=budget)
            if json.loads(payload).get("command") == "validate":
                previous = raw
            return raw

        monkeypatch.setattr(session._backend, "_exchange", capture)
        assert session.validate(_data()).report.verified
        assert previous is not None
        session._backend.reset()
        monkeypatch.setattr(
            session._backend, "_exchange", lambda *args, **kwargs: previous
        )
        invalid = session.validate(_data("bad"))
        assert invalid.conforms is None
        assert invalid.report.outcome == "input_error"
        monkeypatch.setattr(session._backend, "_exchange", exchange)
        assert session.validate(_data("bad")).conforms is False
        assert session.worker_launches == 3


def test_batch_budgets_preserve_completed_results_and_pending_required_scope() -> None:
    with ShaclSession(_shapes(), limits=Limits(max_resources=6)) as session:
        batch = session.validate_batch((_data(),) * 5)
        assert [result.conforms for result in batch.results] == [True, True, True, None]
        assert batch.report.outcome == "exhausted" and not batch.report.complete
        assert "not started" in batch.report.issues[-1].message
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 1
    with ShaclSession(_shapes(), limits=Limits(max_issues=1)) as session:
        batch = session.validate_batch((_data("bad"),) * 4)
        assert len(batch.results) == 2 and batch.results[0].conforms is False
        assert batch.report.outcome == "exhausted" and not batch.report.complete


def test_prepared_functions_are_registered_and_cleaned_independently_per_job() -> None:
    from bijux_ontology.shacl import (
        ShaclConstraintRequirement,
        ShaclFunctionCatalog,
        ShaclFunctionParameter,
        ShaclSparqlFunction,
    )

    function = ShaclSparqlFunction(
        str(EX.double),
        (ShaclFunctionParameter(str(EX.x)),),
        select="SELECT ($x * 2 AS ?result) WHERE {}",
    )
    catalog = ShaclFunctionCatalog.from_functions((function,), trusted=True)
    shapes = (
        _shapes()
        .add((EX.Shape, SH.sparql, EX.Query))
        .add(
            (
                EX.Query,
                SH.select,
                Literal(
                    f"SELECT $this WHERE {{ $this <{EX.value}> ?v . "
                    f"FILTER(<{EX.double}>(?v) != 4) }}"
                ),
            )
        )
    )
    with ShaclSession(
        shapes,
        trusted_sparql=True,
        functions=catalog,
        required_constraints=(
            ShaclConstraintRequirement(str(EX.Shape), str(SH.sparql)),
        ),
    ) as session:
        for value in (2, 3, 2):
            result = session.validate(_data(value))
            assert result.conforms is (value == 2)
            assert dict(result.function_calls) == {str(EX.double): 1}
            assert result.constraints is not None and result.constraints.verified
        assert session.worker_launches == 1


def test_close_stops_active_backend_and_rejects_queued_and_later_calls() -> None:
    session = ShaclSession(_slow_shapes(), trusted_sparql=True, timeout=5)
    try:
        assert session.validate(_data()).report.verified
        with ThreadPoolExecutor(max_workers=1) as executor:
            active = executor.submit(session.validate, _slow_data())
            time.sleep(0.1)
            session.close()
            assert active.result(timeout=3).conforms is None
        assert session._backend._process is None
        with pytest.raises(InputError, match="closed"):
            session.validate(_data())
    finally:
        session.close()


def test_batch_combines_input_and_output_byte_bounds_without_losing_pending_scope() -> (
    None
):
    data = _data().add((EX.one, EX.unchecked, Literal("x" * 1000)))
    with ShaclSession(_shapes(), limits=Limits(max_total_bytes=500_000)) as session:
        batch = session.validate_batch((data,) * 6)
        assert 1 < len(batch.results) < 6
        assert batch.results[0].report.verified
        assert batch.report.outcome == "exhausted" and not batch.report.complete
        assert "not started" in batch.report.issues[-1].message


def test_oversized_frame_reaps_worker_descendants_before_a_fresh_real_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import sys
    from collections.abc import Sequence
    from typing import Any

    original = subprocess.Popen
    first = True
    pid_file = tmp_path / "descendant.pid"
    program = (
        "import pathlib,struct,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "sys.stdout.buffer.write(struct.pack('>Q',2**64-1)); "
        "sys.stdout.buffer.flush(); time.sleep(120)"
    )

    def start(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Launch a real broken peer once, then the real maintained worker."""
        nonlocal first
        if first:
            first = False
            return original([sys.executable, "-c", program, str(pid_file)], **kwargs)
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", start)
    with ShaclSession(_shapes()) as session:
        failed = session.validate(_data())
        assert failed.report.outcome == "exhausted" and failed.conforms is None
        child = int(pid_file.read_text())
        status = subprocess.run(
            ["ps", "-o", "state=", "-p", str(child)],
            capture_output=True,
            check=False,
            text=True,
        ).stdout.strip()
        assert not status or status.startswith("Z")
        assert session.validate(_data()).report.verified
        assert session.worker_launches == 2


def test_batch_captures_bounded_selection_before_worker_startup() -> None:
    selected = [_data(), _data("invalid")]
    with ShaclSession(_shapes()) as session:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(session.validate_batch, selected)
            deadline = time.monotonic() + 5
            while not session.worker_launches and time.monotonic() < deadline:
                time.sleep(0.001)
            assert session.worker_launches == 1
            selected.clear()
            result = pending.result(timeout=5)
        assert len(result.results) == 2
        assert result.results[0].conforms is True
        assert result.results[1].conforms is False
        assert result.report.complete and not result.report.verified


def test_shared_constructor_and_calls_use_one_budget_without_double_issue_charges() -> (
    None
):
    """Native failures remain paid once and fixed sessions do not renew grants."""
    budget = Budget(Limits(max_issues=1))
    with ShaclSession(_shapes(), budget=budget) as session:
        prepared = dict(budget._counts)
        assert prepared["work"] > 0 and prepared["states"] > 0
        assert prepared["bytes"] > 0
        first = session.validate(_data("wrong"))
        assert first.conforms is False and first.report.outcome == "disagreement"
        assert budget._counts["issues"] == 1
        assert budget._counts["work"] > prepared["work"]
        second = session.validate(_data("wrong"))
        assert second.report.outcome == "exhausted"
        assert budget._counts["issues"] == 1
        assert all(
            issue is not first.report.issues[0] for issue in second.report.issues
        )
    with ShaclSession(_shapes(), limits=Limits(max_issues=1)) as independent:
        assert independent.validate(_data("wrong")).report.outcome == "disagreement"
        assert independent.validate(_data("wrong")).report.outcome == "disagreement"
        assert independent.worker_launches == 1


def test_preconsumed_budget_cannot_prepare_or_launch_unpaid_session_work() -> None:
    """Preparation and later data adoption honor the actual remaining allowance."""
    starved = Budget(Limits(max_states=500))
    starved.consume("states", 499)
    with pytest.raises(LimitExceeded):
        ShaclSession(_shapes(), budget=starved)
    oversized = ShaclCoverageContract(focus_nodes=("urn:required:" + "x" * 500_000,))
    with pytest.raises(LimitExceeded):
        ShaclSession(
            _shapes(), budget=Budget(Limits(max_bytes=4096)), coverage=oversized
        )
    budget = Budget()
    with ShaclSession(_shapes(), budget=budget) as session:
        budget.consume("states", budget.remaining("states") - 1)
        stopped = session.validate(_data())
        assert stopped.report.outcome == "exhausted" and stopped.conforms is None
        assert session.worker_launches == 0
        with pytest.raises(InputError, match="session-owned"):
            session._validate(
                _data(),
                focus_nodes=(),
                graph_name=None,
                union=False,
                source_usages=(),
                scope="dataset",
                cancel=None,
                budget=Budget(budget.limits),
            )
    with pytest.raises(InputError, match="matching limits"):
        ShaclSession(_shapes(), budget=Budget(), limits=Limits(max_states=10))


def test_late_coverage_cut_keeps_paid_negative_and_raw_report() -> None:
    """A real report is retained when its later required coverage cannot finish."""
    budget = Budget(Limits(max_resources=2))
    with ShaclSession(
        _shapes(),
        budget=budget,
        coverage=ShaclCoverageContract(minimum_focus=1),
    ) as session:
        result = session.validate(_data("wrong"))
        assert result.conforms is False and result.report.outcome == "exhausted"
        assert not result.report.complete
        assert any(
            issue.code == "shacl.nonconformance" for issue in result.report.issues
        )
        assert result.findings and len(result.report_graph()) > 0
        assert (
            sum(
                issue.code == "shacl.nonconformance"
                for issue in budget._retained_issues.values()
            )
            == 1
        )
