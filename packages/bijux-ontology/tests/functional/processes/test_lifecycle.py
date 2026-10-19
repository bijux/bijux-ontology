"""Owned group cleanup survives zombie races without hiding persistent failures."""

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rdflib import Graph

import bijux_ontology.processes.ownership as processes
from bijux_ontology.interfaces.execution.transport import stop_process
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.queries.access import QueryScope, query_rdf
from bijux_ontology.shacl import transport as shacl_transport

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="Requires POSIX fault workers"
)


def _exited_unreaped(process: subprocess.Popen[bytes]) -> None:
    """Establish exit without releasing the owned leader's PID."""
    deadline = time.monotonic() + 3
    while (
        cast(Any, os).waitid(
            os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
        )
        is None
    ):
        assert time.monotonic() < deadline
        time.sleep(0.001)


@pytest.mark.skipif(
    not callable(getattr(os, "waitid", None)) or not hasattr(os, "WNOWAIT"),
    reason="Requires unreaped exit observation",
)
@pytest.mark.parametrize(
    "stop",
    [
        processes.stop_owned_process,
        shacl_transport._stop_group,
        stop_process,
    ],
)
def test_real_exited_unreaped_group_is_reaped_without_permission_failure(
    stop: Any,
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        _exited_unreaped(process)
        stop(process)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


@pytest.mark.parametrize("transient_denial", [False, True])
def test_exited_leader_does_not_skip_term_ignoring_descendant(
    tmp_path: Path,
    transient_denial: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final escalation proves cleanup even after a transient TERM group error."""
    child_file = tmp_path / "child.pid"
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )
    leader = (
        "import subprocess,sys,time; from pathlib import Path; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"p=Path({str(child_file)!r}); "
        "exec('while not p.exists(): time.sleep(.001)')"
    )
    process = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)
    child_pid: int | None = None
    sentinel = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    real_killpg = os.killpg

    def denied_term(group: int, sig: int) -> None:
        """Deny the owned group's graceful attempt, leaving real escalation intact."""
        if group == process.pid and sig == signal.SIGTERM:
            raise PermissionError("transient graceful group permission denial")
        real_killpg(group, sig)

    if transient_denial:
        monkeypatch.setattr(os, "killpg", denied_term)
    try:
        process.wait(timeout=3)
        child_pid = int(child_file.read_text())
        os.kill(child_pid, 0)
        processes.stop_owned_process(process)
        assert process.returncode == 0 and sentinel.poll() is None
        deadline = time.monotonic() + 2
        while True:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline
            time.sleep(0.005)
    finally:
        sentinel.kill()
        sentinel.wait(timeout=1)
        if child_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)


@pytest.mark.parametrize("overflow", [False, True, "exited"])
def test_persistent_group_denial_stays_visible_and_preserves_primary_outcome(
    overflow: bool | str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = subprocess.Popen
    launch_count = 0
    owned: list[subprocess.Popen[bytes]] = []
    program = (
        "import sys,time; sys.stdout.write('x'*20000); "
        "sys.stdout.flush(); time.sleep(30)"
        if overflow is True
        else "import sys; sys.stdout.write('x'*20000); sys.stdout.flush()"
        if overflow == "exited"
        else 'print(\'{"status":"result","kind":"ASK","answer":true,"complete":true}\')'
    )

    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        nonlocal launch_count
        launch_count += 1
        result = original(
            [sys.executable, "-c", program] if launch_count == 1 else args, **kwargs
        )
        owned.append(result)
        return result

    real_killpg = os.killpg

    def denied(group: int, sig: int) -> None:
        if owned and group == owned[0].pid:
            raise PermissionError("injected live group permission denial")
        real_killpg(group, sig)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", denied)
    failed = query_rdf(
        Graph(),
        "ASK {}",
        scope=QueryScope("default"),
        limits=Limits(max_total_bytes=5000),
    )
    assert not failed.report.verified and not failed.report.complete
    assert failed.report.outcome == ("exhausted" if overflow else "input_error")
    assert "cleanup failed" in failed.report.issues[0].message
    if overflow:
        assert "output exceeds" in failed.report.issues[0].message
    assert owned[0].poll() is not None
    recovered = query_rdf(Graph(), "ASK {}", scope=QueryScope("default"))
    assert recovered.report.verified and recovered.answer is True


def test_direct_worker_cleanup_needs_no_posix_kill_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = (
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(30)"
    )
    process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.PIPE)
    try:
        assert process.stdout is not None and process.stdout.readline() == b"ready\n"
        monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))
        monkeypatch.setattr(processes, "signal", SimpleNamespace())
        processes.stop_owned_process(process)
        assert process.returncode is not None and process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


@pytest.mark.parametrize("consumer", ["api", "cli"])
def test_normal_exit_cleanup_denial_retains_completed_findings(
    consumer: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bijux_ontology.interfaces.cli.execution import execute_bounded
    from bijux_ontology.interfaces.execution.executor import WorkerExecutor

    program = """import sys
from pathlib import Path
from bijux_ontology.interfaces.execution.checkpoints import retain_completed
from bijux_ontology.reports import Issue, Report
retain_completed(Report('completed', 1, (Issue('vocabulary.term_missing',
    'retained finding'),)), Path(sys.argv[1]).parent, max_bytes=50000)
"""
    real_popen, real_killpg = subprocess.Popen, os.killpg
    owned: list[subprocess.Popen[bytes]] = []

    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        request = next(str(arg) for arg in args if str(arg).endswith("request.json"))
        process = real_popen([sys.executable, "-c", program, request], **kwargs)
        owned.append(process)
        return process

    def denied(group: int, sig: int) -> None:
        if owned and group == owned[0].pid:
            raise PermissionError("persistent group denial")
        real_killpg(group, sig)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", denied)
    try:
        result = (
            WorkerExecutor(artifacts=tmp_path).run("vocabulary", {})
            if consumer == "api"
            else execute_bounded(["vocabulary"], timeout=3, limits=Limits())
        )
        assert result.report.outcome == "input_error" and not result.report.verified
        assert any(
            issue.code == "vocabulary.term_missing" for issue in result.report.issues
        )
        assert any("cleanup failed" in issue.message for issue in result.report.issues)
    finally:
        for process in owned:
            with suppress(ProcessLookupError, PermissionError):
                real_killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)


@pytest.mark.parametrize(
    "failure,pipe", [("exhausted", None), ("exhausted", "stderr"), ("cancel", "stdout")]
)
def test_shacl_failed_results_and_held_pipes_remain_bounded(
    failure: str,
    pipe: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bijux_ontology.shacl.session import ShaclSession

    ready = tmp_path / "ready"
    child = (
        "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']"
        + (",stdout=subprocess.DEVNULL" if pipe == "stderr" else "")
        + ")"
        if pipe is not None
        else "pass"
    )
    response = (
        "respond({'status':'exhausted','message':'retained backend exhaustion'})"
        if failure == "exhausted"
        else "pass"
    )
    program = f"""import json,struct,subprocess,sys,time
from pathlib import Path
def read():
    size=struct.unpack('>Q',sys.stdin.buffer.read(8))[0]
    return sys.stdin.buffer.read(size)
def respond(value):
    raw=json.dumps(value).encode()
    sys.stdout.buffer.write(struct.pack('>Q',len(raw))+raw)
    sys.stdout.buffer.flush()
read(); respond({{'status':'ready'}}); read()
{child}
Path({str(ready)!r}).write_text('ready')
{response}
time.sleep(30)
"""
    real_popen, real_killpg = subprocess.Popen, os.killpg
    owned: list[subprocess.Popen[bytes]] = []

    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(
            [sys.executable, "-c", program] if not owned else args, **kwargs
        )
        owned.append(process)
        return process

    def denied(group: int, sig: int) -> None:
        if owned and group == owned[0].pid:
            raise PermissionError("persistent group denial")
        real_killpg(group, sig)

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(os, "killpg", denied)
    shapes = Graph().parse(
        data="""@prefix sh:<http://www.w3.org/ns/shacl#>.
        <urn:shape> a sh:NodeShape; sh:targetNode <urn:r>; sh:nodeKind sh:IRI.
    """,
        format="turtle",
    )
    session = ShaclSession(shapes, timeout=3)
    cancel = threading.Event()
    results: list[Any] = []
    job = threading.Thread(
        target=lambda: results.append(session.validate(Graph(), cancel=cancel)),
        daemon=True,
    )
    try:
        job.start()
        deadline = time.monotonic() + 3
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        if failure == "cancel":
            cancel.set()
        job.join(timeout=3)
        assert not job.is_alive(), "Failed cleanup blocked on a buffered pipe close"
        assert results[0].report.outcome == "exhausted"
        messages = " ".join(issue.message for issue in results[0].report.issues)
        assert "cleanup failed" in messages
        assert (
            "retained backend exhaustion" if failure == "exhausted" else "cancelled"
        ) in messages
        if pipe is None:
            assert session.validate(Graph()).report.verified
    finally:
        for process in owned:
            with suppress(ProcessLookupError, PermissionError):
                real_killpg(process.pid, signal.SIGKILL)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=1)
        job.join(timeout=2)
        session.close()


def test_one_shot_shacl_timeout_reaps_its_real_nested_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one-shot boundary owns descendants even after its leader exits."""
    from bijux_ontology.errors import LimitExceeded

    ready = tmp_path / "descendant.pid"
    child = (
        "import os,signal,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"Path({str(ready)!r}).write_text(str(os.getpid()));time.sleep(30)"
    )
    program = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(30)"
    )
    original = subprocess.Popen
    owned: list[subprocess.Popen[bytes]] = []

    def launch(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        process = original([sys.executable, "-c", program], **kwargs)
        owned.append(process)
        deadline = time.monotonic() + 3
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        return process

    monkeypatch.setattr(subprocess, "Popen", launch)
    try:
        with pytest.raises(LimitExceeded):
            shacl_transport.execute_backend(b"input", cap=4096, timeout=0.05)
        monkeypatch.setattr(subprocess, "Popen", original)
        child_pid = int(ready.read_text())
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(child_pid)],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert not status or status.startswith("Z")
        assert all(process.poll() is not None for process in owned)
    finally:
        monkeypatch.setattr(subprocess, "Popen", original)
        for process in owned:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)


def test_owned_query_module_cannot_be_replaced_by_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actual installed native worker ignores a same-named local package."""
    from bijux_ontology.rdf.queries import QueryScope, query_rdf

    package = tmp_path / "bijux_ontology" / "rdf" / "queries"
    package.mkdir(parents=True)
    for directory in (package, package.parent, package.parent.parent):
        (directory / "__init__.py").write_text("")
    marker = tmp_path / "unexpected-worker"
    (package / "worker.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('unexpected module executed')\n"
    )
    monkeypatch.chdir(tmp_path)
    result = query_rdf(Graph(), "ASK {}", scope=QueryScope("default"))
    assert result.report.verified and result.answer is True
    assert not marker.exists()
