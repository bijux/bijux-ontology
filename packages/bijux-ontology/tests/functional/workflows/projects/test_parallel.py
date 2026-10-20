"""Actual independent validators overlap without losing required ordered scopes."""

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from types import FrameType
from typing import Literal, cast

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    BudgetKind,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.execution.workers import execute_worker
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.parallel import checking as parallel_runtime
from bijux_ontology.workflows.projects.parallel.checking import check_project_parallel
from bijux_ontology.workflows.projects.parallel.types import ParallelJobResult
from bijux_ontology.workflows.projects.resources import ProjectResources

EX = "https://example.org/"
LIMITS = Limits(max_work=10_000_000, max_total_bytes=200_000_000, max_states=1_000_000)


def project(
    path: Path, *, missing: bool = False, shape_timeout: float = 10.0
) -> ProjectConfig:
    """Own ordinary project files with real vocabulary/context/shape obligations."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "vocab.ttl").write_text(
        "\n".join(
            f"<{EX}C{i}> a <http://www.w3.org/2002/07/owl#Class> ." for i in range(60)
        )
    )
    (path / "inventory.json").write_text(
        json.dumps([EX + "Missing"] if missing else [EX + f"C{i}" for i in range(60)])
    )
    (path / "context.json").write_text(
        json.dumps({"@context": {"title": EX + "title"}})
    )
    (path / "document.json").write_text(
        json.dumps({"@context": EX + "context", "title": "Example"})
    )
    (path / "expectations.json").write_text(
        json.dumps([{"path": ["title"], "predicate": EX + "title"}])
    )
    (path / "data.ttl").write_text(f'<{EX}record> <{EX}title> "Example" .')
    (path / "shapes.ttl").write_text(
        f"@prefix sh:<http://www.w3.org/ns/shacl#>. <{EX}Shape> a sh:NodeShape;"
        f" sh:targetNode <{EX}record>; sh:property "
        f"[sh:path <{EX}title>; sh:minCount 1]."
    )
    return ProjectConfig(
        {
            "version": 2,
            "limits": asdict(LIMITS),
            "rdf": {
                "terms": {"path": "vocab.ttl"},
                "data": {"path": "data.ttl"},
                "shapes": {"path": "shapes.ttl"},
            },
            "contexts": {EX + "context": "context.json"},
            "checks": [
                {
                    "name": "terms",
                    "kind": "vocabulary",
                    "source": "terms",
                    "inventory": "inventory.json",
                },
                {
                    "name": "context",
                    "kind": "context",
                    "document": "document.json",
                    "expectations": "expectations.json",
                },
                {
                    "name": "shapes",
                    "kind": "shacl",
                    "document": "data",
                    "shapes": "shapes",
                    "timeout": shape_timeout,
                },
            ],
        },
        directory=path,
    )


def test_actual_checks_overlap_and_keep_sequential_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("pyld")
    pytest.importorskip("pyshacl")
    config = project(tmp_path / "inputs")
    ordinary = check_project(config)
    delivered: list[str] = []
    rendezvous = threading.Barrier(3)

    def concurrent_workers(
        module: str,
        payload: bytes,
        *,
        cap: int,
        timeout: float | None,
        cancel: threading.Event | None = None,
        completed: Callable[[bytes], None] | None = None,
    ) -> bytes:
        rendezvous.wait(timeout=5)
        return execute_worker(
            module,
            payload,
            cap=cap,
            timeout=timeout,
            cancel=cancel,
            completed=completed,
        )

    with monkeypatch.context() as scheduled:
        scheduled.setattr(parallel_runtime, "execute_worker", concurrent_workers)
        result = check_project_parallel(
            config,
            workers=3,
            artifacts=tmp_path / "runs",
            completed=lambda job: delivered.append(job.name),
        )
    assert result.report.verified, result.report.to_dict()
    assert delivered == ["terms", "context", "shapes"]
    assert [job.report.scope for job in result.jobs] == delivered
    assert [job.report.outcome for job in result.jobs] == [
        child.outcome for child in ordinary.children
    ]
    intervals = [(job.started_ns, job.finished_ns) for job in result.jobs]
    assert all(a is not None and b is not None and a <= b for a, b in intervals)
    assert not rendezvous.broken and rendezvous.n_waiting == 0
    assert all(dict(job.consumed)["work"] > 0 for job in result.jobs)
    assert not list((tmp_path / "runs").iterdir())

    independent = tmp_path / "independent-inputs"
    independent.mkdir()
    for name in ("small", "large"):
        (independent / f"{name}.ttl").write_text(
            ("#" + "x" * 150_000 + "\n" if name == "large" else "")
            + f"<{EX}{name}> a <http://www.w3.org/2002/07/owl#Class>."
        )
        (independent / f"{name}.json").write_text(json.dumps([EX + name]))
    separate = ProjectConfig(
        {
            "version": 2,
            "limits": asdict(replace(LIMITS, max_work=2_000_000)),
            "rdf": {name: {"path": f"{name}.ttl"} for name in ("small", "large")},
            "checks": [
                {
                    "name": name,
                    "kind": "vocabulary",
                    "source": name,
                    "inventory": f"{name}.json",
                }
                for name in ("small", "large")
            ],
        },
        directory=independent,
    )
    isolated = check_project_parallel(separate, workers=2, artifacts=tmp_path / "lazy")
    assert isolated.jobs[0].report.verified, isolated.report.to_dict()
    assert isolated.jobs[1].report.outcome == "exhausted"


def test_failed_or_timed_out_dependencies_do_not_hide_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project(tmp_path / "inputs", missing=True, shape_timeout=0.000001)
    raw = config.to_dict()
    cast(list[object], raw["checks"]).append(
        {
            "name": "dependent",
            "kind": "vocabulary",
            "source": "terms",
            "inventory": "inventory.json",
            "depends_on": ["shapes"],
        }
    )
    result = check_project_parallel(
        ProjectConfig(raw, directory=config.directory),
        workers=3,
        artifacts=tmp_path / "runs",
    )
    assert any(
        issue.code == "vocabulary.term_missing"
        for issue in result.jobs[0].report.issues
    )
    assert not result.jobs[2].report.verified
    assert (
        result.jobs[3].state == "pending"
        and result.jobs[3].reason == "dependency-unverified"
    )
    assert not result.report.verified
    (config.directory / "inventory.json").unlink()
    missing = check_project_parallel(config, artifacts=tmp_path / "missing")
    assert missing.jobs[0].report.outcome == "input_error"
    assert missing.jobs[1].report.verified
    if hasattr(os, "mkfifo"):
        fifo_config = project(tmp_path / "fifo-inputs")
        inventory = fifo_config.directory / "inventory.json"
        inventory.unlink()
        os.mkfifo(inventory)
        before = time.monotonic()
        fifo = check_project_parallel(
            fifo_config, timeout=10, artifacts=tmp_path / "fifo-runs"
        )
        assert time.monotonic() - before < 5
        assert not fifo.jobs[0].report.verified
        assert fifo.jobs[1].report.verified and fifo.jobs[2].report.verified

    if os.name != "posix":
        return
    original_shapes = (config.directory / "shapes.ttl").read_bytes()
    original_data = (config.directory / "data.ttl").read_bytes()
    (config.directory / "inventory.json").write_text(json.dumps([EX + "Missing"]))
    (config.directory / "data.ttl").write_text(
        "\n".join(f"<{EX}n{i}> <urn:number> {i} ." for i in range(100))
    )
    (config.directory / "shapes.ttl").write_text(
        f"@prefix sh:<http://www.w3.org/ns/shacl#>. <{EX}Shape> a sh:NodeShape; "
        f'sh:targetNode <{EX}record>; sh:sparql [sh:select """SELECT $this WHERE {{ '
        "?a <urn:number> ?b . ?c <urn:number> ?d . ?e <urn:number> ?f "
        'FILTER ((?b + ?d + ?f) < 0) }"""].'
    )
    raw = config.to_dict()
    shapes = {**config.checks()[2], "timeout": 30.0, "trusted_sparql": True}
    raw["checks"] = [
        config.checks()[0],
        shapes,
        {
            "name": "dependent",
            "kind": "vocabulary",
            "source": "terms",
            "inventory": "inventory.json",
            "depends_on": ["shapes"],
        },
    ]
    actual = ProjectConfig(raw, directory=config.directory)
    cancel, observed_stop = threading.Event(), threading.Event()
    received = threading.Event()
    actual_reply = parallel_runtime._reply

    def observe_reply(
        raw: bytes, name: str, grant: dict[BudgetKind, int]
    ) -> ParallelJobResult:
        """Observe actual qualified findings rather than guessing which PID exited."""
        result = actual_reply(raw, name, grant)
        if name == "terms" and any(
            issue.code == "vocabulary.term_missing" for issue in result.report.issues
        ):
            received.set()
        return result

    monkeypatch.setattr(parallel_runtime, "_reply", observe_reply)
    owned: set[int] = set()
    nested: list[int] = []

    def watch_owned_workers() -> None:
        """Observe actual process ownership and cancel while SHACL is running."""
        while not observed_stop.wait(0.01):
            rows = [
                line.strip().split(None, 2)
                for line in subprocess.check_output(
                    ["ps", "-ww", "-axo", "pid=,ppid=,command="], text=True
                ).splitlines()
            ]
            live = {
                int(pid)
                for pid, parent, command in rows
                if int(parent) == os.getpid()
                and "bijux_ontology.workflows.projects.parallel.worker" in command
            }
            owned.update(live)
            nested.extend(
                int(pid)
                for pid, parent, command in rows
                if int(parent) in owned
                and "bijux_ontology.shacl.execution.worker" in command
            )
            if received.is_set() and len(owned) >= 2 and len(live) == 1:
                cancel.set()
                return

    watcher = threading.Thread(target=watch_owned_workers, daemon=True)
    watcher.start()
    try:
        cancelled = check_project_parallel(
            actual, workers=2, timeout=10, cancel=cancel, artifacts=tmp_path / "owned"
        )
    finally:
        observed_stop.set()
        watcher.join(timeout=2)
    assert cancel.is_set() and owned and not nested
    assert any(
        issue.code == "vocabulary.term_missing"
        for issue in cancelled.jobs[0].report.issues
    )
    assert cancelled.jobs[1].state == "interrupted"
    assert cancelled.jobs[2].state == "pending"
    for pid in owned:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    (config.directory / "shapes.ttl").write_bytes(original_shapes)
    (config.directory / "data.ttl").write_bytes(original_data)
    recovery_data = config.to_dict()
    cast(list[dict[str, object]], recovery_data["checks"])[2]["timeout"] = 10.0
    recovery = check_project_parallel(
        ProjectConfig(recovery_data, directory=config.directory),
        artifacts=tmp_path / "recovered",
    )
    assert recovery.jobs[2].report.verified


def test_shared_accounting_preserves_first_finding_and_never_restarts(
    tmp_path: Path,
) -> None:
    config = project(tmp_path / "inputs", missing=True)
    raw = config.to_dict()
    raw["limits"] = asdict(replace(LIMITS, max_issues=1))
    config = ProjectConfig(raw, directory=config.directory)
    budget = Budget(config.limits)
    result = check_project_parallel(
        config, workers=3, budget=budget, artifacts=tmp_path / "runs"
    )
    assert any(
        issue.code == "vocabulary.term_missing" for issue in result.report.issues
    )
    for kind in ("bytes", "work", "issues", "states", "resources", "triples"):
        assert sum(dict(job.consumed).get(kind, 0) for job in result.jobs) <= getattr(
            config.limits, "max_total_bytes" if kind == "bytes" else "max_" + kind
        ) - budget.remaining(kind)
    assert budget.remaining("issues") == 0
    adopted = IssueCollector(budget)
    retain_report(result.report, budget, adopted)
    assert sum(issue.kind == "finding" for issue in adopted) == 1
    assert budget.remaining("issues") == 0
    context_raw = config.to_dict()
    context_raw["checks"] = [config.checks()[1]]
    (config.directory / "expectations.json").write_text(
        json.dumps([{"path": ["title"], "predicate": EX + "wrong"}])
    )
    context = check_project_parallel(
        ProjectConfig(context_raw, directory=config.directory),
        artifacts=tmp_path / "context-cap",
    )
    assert any(issue.kind == "finding" for issue in context.report.issues)
    again = check_project_parallel(
        config, workers=3, budget=budget, artifacts=tmp_path / "again"
    )
    assert not again.report.verified
    cancelled = threading.Event()
    cancelled.set()
    stopped = check_project_parallel(
        config, artifacts=tmp_path / "stopped", cancel=cancelled
    )
    assert stopped.report.outcome == "exhausted" and all(
        job.state == "pending" for job in stopped.jobs
    )
    fresh = check_project_parallel(config, artifacts=tmp_path / "fresh")
    assert any(issue.code == "vocabulary.term_missing" for issue in fresh.report.issues)
    limited_data = config.to_dict()
    limited_data["limits"] = asdict(
        replace(LIMITS, max_bytes=1000, max_total_bytes=5000, max_work=100_000)
    )
    for path in config.input_paths():
        path.write_bytes(b"x" * 2000)
    limited = ProjectConfig(limited_data, directory=config.directory)
    physical = Budget(limited.limits)
    exhausted = check_project_parallel(
        limited, budget=physical, artifacts=tmp_path / "physical"
    )
    assert exhausted.report.outcome == "exhausted"
    assert physical.remaining("bytes") < physical.limits.max_bytes


def test_captured_original_paths_base_pin_and_context_are_immutable(
    tmp_path: Path,
) -> None:
    config = project(tmp_path / "inputs")
    raw = config.to_dict()
    cast(dict[str, object], raw["rdf"])["data"] = {
        "path": "data.trig",
        "format": "trig",
        "iri": EX + "document",
        "graph_name": EX + "G",
    }
    (config.directory / "data.trig").write_text(
        f'<{EX}G> {{ <record> <{EX}title> "Example" }} '
        f'<{EX}H> {{ <other> <{EX}title> "Hidden" }}'
    )
    config = ProjectConfig(raw, directory=config.directory)
    captured = {path: path.read_bytes() for path in config.input_paths()}
    resources = ProjectResources(
        config, budget=Budget(config.limits), captured=captured
    )
    for path in config.input_paths():
        path.write_text("invalid overwritten input")
    source = resources.source("data")
    assert source.graph_name == EX + "G" and len(source.graph()) == 1
    assert str(next(source.graph().subjects())).startswith(EX)
    document = resources.read_json(config.directory / "document.json")
    assert isinstance(document, dict) and document["title"] == "Example"
    loaded = resources.document_loader(EX + "context", {})
    document = loaded["document"]
    assert isinstance(document, dict)
    assert document["@context"]["title"] == EX + "title"
    with pytest.raises(InputError, match="not captured"):
        resources.read_json(config.directory / "unregistered.json")
    runtime = project(tmp_path / "runtime")
    runs = tmp_path / "captured-runs"
    stop, mutated = threading.Event(), threading.Event()

    def mutate_originals() -> None:
        """Change originals only after the private captured manifest exists."""
        while not stop.wait(0.001):
            if list(runs.glob("project-checks-*/project.json")):
                for path in runtime.input_paths():
                    path.write_text("invalid replacement after capture")
                mutated.set()
                return

    observer = threading.Thread(target=mutate_originals, daemon=True)
    observer.start()
    try:
        result = check_project_parallel(runtime, workers=3, artifacts=runs)
    finally:
        stop.set()
        observer.join(timeout=1)
    assert mutated.is_set() and result.report.verified, result.report.to_dict()

    links = project(tmp_path / "links", missing=True)
    inventory = links.directory / "inventory.json"
    good = links.directory / "good.json"
    good.write_text(json.dumps([EX + "C0"]))
    raw = links.to_dict()
    raw["checks"] = [
        {"name": name, "kind": "vocabulary", "source": "terms", "inventory": file}
        for name, file in (("missing", "inventory.json"), ("good", "good.json"))
    ]
    links = ProjectConfig(raw, directory=links.directory)
    runs = tmp_path / "link-runs"
    stop, mutated = threading.Event(), threading.Event()

    def retarget_original() -> None:
        """A captured path keeps its identity even if its original becomes a link."""
        while not stop.wait(0.001):
            if list(runs.glob("project-checks-*/project.json")):
                inventory.unlink()
                inventory.symlink_to(good)
                mutated.set()
                return

    observer = threading.Thread(target=retarget_original, daemon=True)
    observer.start()
    try:
        result = check_project_parallel(links, workers=2, artifacts=runs)
    finally:
        stop.set()
        observer.join(timeout=1)
    assert mutated.is_set()
    assert any(
        i.code == "vocabulary.term_missing" for i in result.jobs[0].report.issues
    )
    assert result.jobs[1].report.verified


def test_unsupported_revisions_and_jobs_are_required_not_stale(tmp_path: Path) -> None:
    config = project(tmp_path / "inputs")
    with pytest.raises(InputError, match="ProjectConfig"):
        check_project_parallel(
            cast(ProjectConfig, PreparedChecks(config)), artifacts=tmp_path / "runs"
        )
    raw = config.to_dict()
    raw["checks"] = [
        {
            "name": "encoding",
            "kind": "ontology",
            "source": "terms",
        }
    ]
    result = check_project_parallel(
        ProjectConfig(raw, directory=config.directory), artifacts=tmp_path / "runs"
    )
    assert result.jobs[0].state == "unsupported" and not result.report.verified
    before = time.monotonic()
    stopped = check_project_parallel(
        config, timeout=0.000001, artifacts=tmp_path / "deadline"
    )
    assert time.monotonic() - before < 2
    assert stopped.report.outcome == "exhausted"


def test_completed_native_finding_survives_inherited_cancel_at_handoff(
    tmp_path: Path,
) -> None:
    """Cancel at the actual qualified worker return, preserving its paid result."""
    config = project(tmp_path / "inputs", missing=True)
    raw = config.to_dict()
    first = config.checks()[0]
    raw["checks"] = [first, {**first, "name": "later"}]
    config = ProjectConfig(raw, directory=config.directory)
    cancellation = threading.Event()
    observed: list[ParallelJobResult] = []
    previous = threading.getprofile()

    def after_native(
        frame: FrameType,
        event: Literal["call", "return", "c_call", "c_return", "c_exception"],
        result: object,
    ) -> None:
        if previous is not None:
            previous(frame, event, result)
        if (
            event == "return"
            and frame.f_code is parallel_runtime._execute.__code__
            and isinstance(result, ParallelJobResult)
        ):
            observed.append(result)
            cancellation.set()

    threading.setprofile(after_native)
    try:
        result = check_project_parallel(
            config,
            workers=1,
            budget=Operation(config.limits, None, 30, cancellation),
            artifacts=tmp_path / "runs",
        )
    finally:
        threading.setprofile(previous)
    assert len(observed) == 1 and observed[0].state == "completed"
    assert result.jobs[0].state == "completed"
    assert any(
        issue.code == "vocabulary.term_missing"
        for issue in result.jobs[0].report.issues
    )
    assert any(
        issue.code == "vocabulary.term_missing" for issue in result.report.issues
    )
    assert result.jobs[1].state == "pending" and result.report.outcome == "exhausted"


@pytest.mark.parametrize(
    ("missing", "stop_kind"), [(False, "cancel"), (True, "cancel"), (False, "deadline")]
)
def test_completed_native_receipt_survives_late_stop_and_preserves_counters(
    tmp_path: Path, missing: bool, stop_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain actual qualified outcomes once, without cancelling them into success."""
    config = project(tmp_path / "inputs", missing=missing)
    raw = config.to_dict()
    raw["checks"] = [config.checks()[0]]
    config = ProjectConfig(raw, directory=config.directory)
    budget, cancelled = Budget(config.limits), threading.Event()
    actual_exchange = execute_worker
    receipts: list[bytes] = []

    def exchange(
        module: str,
        payload: bytes,
        *,
        cap: int,
        timeout: float,
        cancel: threading.Event | None = None,
        completed: Callable[[bytes], None] | None = None,
    ) -> bytes:
        """Request a real stop only after the real native receipt was qualified."""
        assert completed is not None and cancel is not None
        started = time.monotonic()

        def handoff(response: bytes) -> None:
            """Delegate the unchanged domain decoder before stopping either owner."""
            receipts.append(response)
            completed(response)
            if stop_kind == "cancel":
                cancelled.set()
                cancel.set()
            else:
                time.sleep(max(0.0, started + timeout + 0.1 - time.monotonic()))

        return actual_exchange(
            module, payload, cap=cap, timeout=timeout, cancel=cancel, completed=handoff
        )

    monkeypatch.setattr(parallel_runtime, "execute_worker", exchange)
    result = check_project_parallel(
        config,
        budget=budget,
        artifacts=tmp_path / "runs",
        cancel=cancelled,
        job_timeout=5,
    )
    assert len(receipts) == 1 and isinstance(receipts[0], bytes)
    if stop_kind == "cancel":
        assert result.jobs[0].state == "completed"
        assert result.jobs[0].report.complete
        assert result.jobs[0].report.verified is not missing
    else:
        assert result.jobs[0].state == "interrupted"
        assert not result.jobs[0].report.complete
        assert result.jobs[0].report.children[0].verified
    assert (
        any(
            issue.code == "vocabulary.term_missing"
            for issue in result.jobs[0].report.issues
        )
        is missing
    )
    assert not result.report.complete and not result.report.verified
    assert any(issue.kind == "exhausted" for issue in result.report.issues)
    for kind in ("bytes", "work", "issues", "states", "resources", "triples"):
        maximum = getattr(
            config.limits, "max_total_bytes" if kind == "bytes" else "max_" + kind
        )
        used = maximum - budget.remaining(kind)
        assert 0 <= used <= maximum
        assert sum(dict(job.consumed).get(kind, 0) for job in result.jobs) <= used
    assert budget.remaining("issues") == config.limits.max_issues - int(missing)
    assert not list((tmp_path / "runs").iterdir())
    monkeypatch.setattr(parallel_runtime, "execute_worker", actual_exchange)
    recovered = check_project_parallel(config, artifacts=tmp_path / "recovery")
    assert recovered.report.complete and recovered.report.verified is not missing


def application_project(path: Path) -> ProjectConfig:
    """Share one actual declared record across JSON-LD, RDF and SHACL jobs."""
    original = project(path)
    with (path / "vocab.ttl").open("a") as stream:
        stream.write(
            f"<{EX}Record> a <http://www.w3.org/2002/07/owl#Class>."
            f"<{EX}title> a <http://www.w3.org/1999/02/22-rdf-syntax-ns#Property>."
        )
    (path / "models.json").write_text(
        json.dumps(
            [
                {
                    "identifier": "Record",
                    "types": [EX + "Record"],
                    "fields": [
                        {
                            "name": "title",
                            "predicate": EX + "title",
                            "emission": "guaranteed",
                            "values": [
                                {
                                    "kind": "literal",
                                    "datatype": "http://www.w3.org/2001/XMLSchema#string",
                                }
                            ],
                        }
                    ],
                }
            ]
        )
    )
    (path / "usages.json").write_text('[{"model":"Record","path":[]}]')
    (path / "roots.json").write_text(
        json.dumps([{"model": "Record", "subject": EX + "record"}])
    )
    (path / "document.json").write_text(
        json.dumps(
            {"@context": EX + "context", "@type": EX + "Record", "title": "Example"}
        )
    )
    (path / "data.ttl").write_text(
        f'<{EX}record> a <{EX}Record>; <{EX}title> "Example".'
    )
    data = original.to_dict()
    terms, context, shapes = original.checks()
    data["checks"] = [
        terms,
        {
            "name": "models",
            "kind": "models",
            "source": "terms",
            "declarations": "models.json",
            "depends_on": ["terms"],
        },
        context,
        {
            "name": "output",
            "kind": "emission",
            "source": "terms",
            "declarations": "models.json",
            "usages": "usages.json",
            "document": "document.json",
            "depends_on": ["models"],
        },
        {
            "name": "native",
            "kind": "rdf-emission",
            "document": "data",
            "declarations": "models.json",
            "usages": "roots.json",
            "depends_on": ["models"],
        },
        {**shapes, "coverage": {"pairs": [[EX + "Shape", EX + "record"]]}},
    ]
    return ProjectConfig(data, directory=path)


def test_mixed_contract_jobs_use_captured_files_and_real_parallel_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = application_project(tmp_path / "inputs")
    ordinary = check_project(config)
    assert ordinary.verified, ordinary.to_dict()
    runs = tmp_path / "runs"
    stopped, mutated = threading.Event(), threading.Event()

    def replace_captured_originals() -> None:
        """Delete application inputs only after immutable capture has completed."""
        while not stopped.wait(0.001):
            if list(runs.glob("project-checks-*/project.json")):
                for path in config.input_paths():
                    path.unlink()
                mutated.set()
                return

    observer = threading.Thread(target=replace_captured_originals, daemon=True)
    observer.start()
    budget = Budget(config.limits)
    rendezvous = threading.Barrier(3)
    guard = threading.Lock()
    participants = 0

    def concurrent_workers(
        module: str,
        payload: bytes,
        *,
        cap: int,
        timeout: float | None,
        cancel: threading.Event | None = None,
        completed: Callable[[bytes], None] | None = None,
    ) -> bytes:
        nonlocal participants
        with guard:
            join = participants < 3
            participants += 1
        if join:
            rendezvous.wait(timeout=5)
        return execute_worker(
            module,
            payload,
            cap=cap,
            timeout=timeout,
            cancel=cancel,
            completed=completed,
        )

    try:
        with monkeypatch.context() as scheduled:
            scheduled.setattr(parallel_runtime, "execute_worker", concurrent_workers)
            result = check_project_parallel(
                config, workers=3, budget=budget, artifacts=runs
            )
    finally:
        stopped.set()
        observer.join(timeout=1)
    assert mutated.is_set() and result.report.verified, result.report.to_dict()
    assert [job.name for job in result.jobs] == [
        "terms",
        "models",
        "context",
        "output",
        "native",
        "shapes",
    ]
    assert [job.report.outcome for job in result.jobs] == [
        child.outcome for child in ordinary.children
    ]
    assert not rendezvous.broken and rendezvous.n_waiting == 0 and participants == 6
    assert all(
        job.started_ns is not None
        and job.finished_ns is not None
        and job.started_ns <= job.finished_ns
        for job in result.jobs
    )
    for kind in ("bytes", "work", "issues", "states", "resources", "triples"):
        maximum = getattr(
            config.limits, "max_total_bytes" if kind == "bytes" else "max_" + kind
        )
        assert sum(dict(job.consumed).get(kind, 0) for job in result.jobs) <= (
            maximum - budget.remaining(kind)
        )
