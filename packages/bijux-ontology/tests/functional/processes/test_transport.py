"""Opaque request pipes preserve bytes and bound both worker output streams."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from types import FrameType
from typing import Any, cast

import pytest
from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.processes.sessions import FramedSession, WorkerGeneration
from bijux_ontology.execution.processes.transport import exchange_worker, run_worker
from bijux_ontology.shacl.checking import validate_shapes


@pytest.mark.parametrize("timeout", [3.0, None])
def test_binary_exchange_bounds_stdout_and_stderr_and_recovers(
    timeout: float | None,
) -> None:
    """The same no-shell transport supports nontext requests and clean recovery."""
    payload = b"\x00\xffsupplied bytes\n"
    echo = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; data=sys.stdin.buffer.read(); "
        "sys.stderr.write('diagnostic'); sys.stdout.buffer.write(data)",
    ]
    assert exchange_worker(echo, payload, cap=128, timeout=timeout) == payload
    for stream in ("stdout", "stderr"):
        overflow = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            f"import sys; sys.{stream}.write('x'*1000)",
        ]
        with pytest.raises(LimitExceeded, match="output exceeds"):
            exchange_worker(overflow, b"", cap=32, timeout=timeout)
    cancelled = Event()
    cancelled.set()
    with pytest.raises(LimitExceeded, match="cancelled"):
        exchange_worker(echo, payload, cap=128, timeout=timeout, cancel=cancelled)
    assert exchange_worker(echo, payload, cap=128, timeout=timeout) == payload


def test_diagnostic_workers_admit_only_live_finite_execution() -> None:
    """Cancelled, expired and invalid requests do not create a child process."""
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.stdout.buffer.write(b'completed\\n')",
    ]
    cancelled = Event()
    cancelled.set()
    assert run_worker(command, timeout=3, cap=128, cancellation=cancelled) == (
        None,
        "execution.cancelled",
        "",
    )
    assert run_worker(
        command,
        timeout=3,
        cap=128,
        cancellation=None,
        deadline=lambda: time.monotonic() - 1,
    ) == (None, "execution.deadline", "")
    for timeout in (
        float("nan"),
        float("inf"),
        10**1000,
        0,
        -1,
        True,
        False,
        cast(float, None),
    ):
        with pytest.raises(InputError, match="finite and positive"):
            run_worker(command, timeout=timeout, cap=128, cancellation=None)
        with pytest.raises(InputError, match="finite and positive"):
            Operation(None, None, timeout, None)
    for invalid_deadline in (float("nan"), float("inf"), 10**1000):

        def invalid_probe(value: float = invalid_deadline) -> float:
            """Return the captured invalid host deadline without numeric coercion."""
            return value

        with pytest.raises(InputError, match="finite time"):
            run_worker(
                command,
                timeout=3,
                cap=128,
                cancellation=None,
                deadline=invalid_probe,
            )
    for cap in (0, -1, True):
        with pytest.raises(InputError, match="positive output cap"):
            run_worker(command, timeout=3, cap=cap, cancellation=None)
    assert run_worker(command, timeout=3, cap=128, cancellation=None) == (
        0,
        "",
        "completed\n",
    )


def test_successful_exit_cannot_hide_an_undelivered_request() -> None:
    """Real early pipe closure is a typed failure, including buffered close errors."""
    payload = b"x" * 1000000
    for status in (0, 7):
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            "import os,sys; os.close(0); print('unread'); sys.exit("
            + str(status)
            + ")",
        ]
        with pytest.raises(
            ResourceError,
            match="before delivery" if status == 0 else "Worker exited 7",
        ):
            exchange_worker(command, payload, cap=len(payload), timeout=3)
    assert (
        exchange_worker(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
            ],
            b"complete",
            cap=128,
            timeout=3,
        )
        == b"complete"
    )


def test_real_shacl_report_requires_successful_worker_exit_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine conforming report cannot qualify failed native execution."""
    pytest.importorskip("pyshacl")
    data = Graph().parse(data='<urn:item> <urn:value> "complete".', format="turtle")
    shapes = Graph().parse(
        data="""@prefix sh:<http://www.w3.org/ns/shacl#>.
        <urn:shape> a sh:NodeShape; sh:targetNode <urn:item>; sh:property [
        sh:path <urn:value>; sh:minCount 1;
        sh:datatype <http://www.w3.org/2001/XMLSchema#string>].""",
        format="turtle",
    )
    assert validate_shapes(data, shapes, timeout=None).report.verified
    original = subprocess.Popen
    owned: list[subprocess.Popen[bytes]] = []
    program = """import runpy
try:
    runpy.run_module('bijux_ontology.shacl.execution.worker', run_name='__main__')
except SystemExit as result:
    if result.code not in (0, None):
        raise
raise SystemExit(7)
"""

    def launch(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Replace only the native entry point, retaining owned Windows prefixes."""
        arguments = list(command)
        for index in range(len(arguments) - 1):
            if arguments[index : index + 2] == [
                "-m",
                "bijux_ontology.shacl.execution.worker",
            ]:
                arguments[index : index + 2] = ["-c", program]
                process = original(arguments, **kwargs)
                owned.append(process)
                return process
        return original(arguments, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(subprocess, "Popen", launch)
        failed = validate_shapes(data, shapes)
    assert owned and all(process.returncode == 7 for process in owned)
    assert failed.report.outcome == "unverified" and not failed.report.verified
    assert failed.conforms is None and not failed.report.complete
    assert validate_shapes(data, shapes).report.verified


@pytest.mark.skipif(os.name != "posix", reason="Requires detached POSIX pipe holders")
@pytest.mark.parametrize("binary", [False, True])
@pytest.mark.parametrize("cancelled", [False, True])
def test_exited_leader_cannot_certify_unfinished_pipes(
    binary: bool, cancelled: bool, tmp_path: Path
) -> None:
    """A zero leader exit cannot certify pipes still held outside its group."""
    receipt = tmp_path / "holder.pid"
    holder = (
        "import os,time; from pathlib import Path; "
        f"Path({str(receipt)!r}).write_text(str(os.getpid())); "
        "time.sleep(3); os.write(1,b'LATE'); time.sleep(30)"
    )
    leader = (
        "import subprocess,sys,time; from pathlib import Path; "
        "sys.stdout.buffer.write(b'A'*8192); sys.stdout.buffer.flush(); "
        f"subprocess.Popen([sys.executable,'-I','-B','-c',{holder!r}], "
        "start_new_session=True); "
        f"p=Path({str(receipt)!r}); "
        "exec('while not p.exists(): time.sleep(.001)')"
    )
    command = [sys.executable, "-I", "-B", "-c", leader]
    cancel, finished = Event(), Event()

    def cancel_ready() -> None:
        """Request cancellation only after the actual detached holder exists."""
        while not receipt.exists() and not finished.wait(0.005):
            pass
        if not finished.is_set():
            cancel.set()

    watcher = Thread(target=cancel_ready, daemon=True) if cancelled else None
    if watcher is not None:
        watcher.start()
    try:
        if binary:
            error_type = LimitExceeded if cancelled else ResourceError
            with pytest.raises(error_type) as failed:
                exchange_worker(command, b"", cap=16384, timeout=8, cancel=cancel)
            assert "pipes remain held" in str(failed.value)
            if cancelled:
                assert "cancelled" in str(failed.value)
        elif cancelled:
            _, stopped, detail = run_worker(
                command, cap=16384, timeout=8, cancellation=cancel
            )
            assert stopped == "execution.cancelled"
            assert "pipes remain held" in detail
        else:
            with pytest.raises(ResourceError, match="pipes remain held"):
                run_worker(command, cap=16384, timeout=8, cancellation=None)
        assert receipt.exists()
        os.kill(int(receipt.read_text()), 0)
    finally:
        finished.set()
        if watcher is not None:
            watcher.join(timeout=1)
        if receipt.exists():
            with suppress(ProcessLookupError):
                os.kill(int(receipt.read_text()), signal.SIGKILL)
    assert (
        exchange_worker(
            [sys.executable, "-I", "-B", "-c", "print('complete',end='')"],
            b"",
            cap=128,
            timeout=3,
        )
        == b"complete"
    )


def test_framed_generation_rejects_retired_and_foreign_response_ownership() -> None:
    """An actual completed frame cannot cross a generation replacement boundary."""
    program = """import struct,sys
while True:
 header=sys.stdin.buffer.read(8)
 if not header:break
 size=struct.unpack('>Q',header)[0]
 data=sys.stdin.buffer.read(size)
 sys.stdout.buffer.write(struct.pack('>Q',len(data))+data)
 sys.stdout.buffer.flush()
"""
    command = (sys.executable, "-I", "-B", "-c", program)
    session, foreign = FramedSession(command, cap=128), FramedSession(command, cap=128)
    returned, resume = Event(), Event()
    values: list[bytes] = []
    failures: list[BaseException] = []
    first = session.ensure_generation(time.monotonic() + 5, None)

    def observe(frame: FrameType, event: str, argument: object) -> None:
        # Pause only after real response I/O, before its final ownership check.
        parent = frame.f_back
        if (
            event == "call"
            and frame.f_code.co_name == "checkpoint"
            and parent is not None
            and parent.f_code is FramedSession.exchange.__code__
            and isinstance(parent.f_locals.get("complete"), Event)
            and parent.f_locals["complete"].is_set()
        ):
            returned.set()
            assert resume.wait(10)

    def request() -> None:
        sys.setprofile(observe)
        try:
            values.append(session.exchange(first, b"old", time.monotonic() + 10, None))
        except BaseException as error:
            failures.append(error)
        finally:
            sys.setprofile(None)

    thread = Thread(target=request)
    thread.start()
    try:
        assert returned.wait(5)
        session.reset(generation=first)
        current = session.ensure_generation(time.monotonic() + 5, None)
        assert current is not first and current.number == first.number + 1
        foreign_generation = foreign.ensure_generation(time.monotonic() + 5, None)
        for rejected in (
            WorkerGeneration(current.number, current.pid),
            foreign_generation,
        ):
            with pytest.raises(ResourceError, match="generation"):
                session.exchange(rejected, b"foreign", time.monotonic() + 5, None)
        resume.set()
        thread.join(5)
        assert not thread.is_alive() and not values
        assert len(failures) == 1 and isinstance(failures[0], ResourceError)
        # The late failed request can discard only its original generation.
        assert session.generation is current
        assert (
            session.exchange(current, b"fresh", time.monotonic() + 5, None) == b"fresh"
        )
        assert session.launches == 2
    finally:
        resume.set()
        thread.join(5)
        session.close()
        foreign.close()
    for closed in (session, foreign):
        with pytest.raises(ResourceError, match="closed"):
            closed.ensure_generation(time.monotonic() + 5, None)


@pytest.mark.parametrize("boundary", ["diagnostics", "frame"])
def test_framed_thread_start_failure_reaps_real_worker_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    """A failed thread owner cannot strand a spawned child or its pipes."""
    from bijux_ontology.execution.processes import sessions
    from bijux_ontology.execution.processes.ownership import spawn_owned_process

    spawn = spawn_owned_process
    children: list[subprocess.Popen[bytes]] = []

    def capture(
        command: list[str] | tuple[str, ...],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        process = spawn(command, stdin=stdin, stdout=stdout, stderr=stderr)
        children.append(process)
        return process

    def unavailable(thread: Thread) -> None:
        raise RuntimeError("diagnostic thread unavailable")

    program = (
        "import struct,sys,time; header=sys.stdin.buffer.read(8); "
        "data=sys.stdin.buffer.read(struct.unpack('>Q',header)[0]); "
        "sys.stdout.buffer.write(header+data); sys.stdout.buffer.flush(); "
        "time.sleep(60)"
    )
    session = FramedSession((sys.executable, "-I", "-B", "-c", program), cap=128)
    monkeypatch.setattr(sessions, "spawn_owned_process", capture)
    try:
        first = (
            session.ensure_generation(time.monotonic() + 5, None)
            if boundary == "frame"
            else None
        )
        with monkeypatch.context() as failed:
            failed.setattr(Thread, "start", unavailable)
            with pytest.raises(ResourceError, match=boundary + " could not be started"):
                if first is None:
                    session.ensure_generation(time.monotonic() + 5, None)
                else:
                    session.exchange(first, b"request", time.monotonic() + 5, None)
        assert len(children) == 1 and children[0].poll() is not None
        assert all(
            stream is not None and stream.closed
            for stream in (children[0].stdin, children[0].stdout, children[0].stderr)
        )
        assert session.generation is None and session.launches == 1
        recovered = session.ensure_generation(time.monotonic() + 5, None)
        assert recovered.number == 2
        assert (
            session.exchange(recovered, b"fresh", time.monotonic() + 5, None)
            == b"fresh"
        )
    finally:
        session.close()
    assert len(children) == 2 and all(child.poll() is not None for child in children)


def test_framed_diagnostics_bound_interrupts_live_worker_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial diagnostic block cannot hide overflow until response or EOF."""
    from bijux_ontology.execution.processes import sessions
    from bijux_ontology.execution.processes.ownership import spawn_owned_process

    spawn = spawn_owned_process
    children: list[subprocess.Popen[bytes]] = []

    def capture(
        command: list[str] | tuple[str, ...],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
    ) -> subprocess.Popen[bytes]:
        process = spawn(command, stdin=stdin, stdout=stdout, stderr=stderr)
        children.append(process)
        return process

    marker = tmp_path / "diagnostic-request"
    program = """import os,struct,sys
from pathlib import Path
while True:
 header=sys.stdin.buffer.read(8)
 if not header:break
 data=sys.stdin.buffer.read(struct.unpack('>Q',header)[0])
 if data==b'overflow':
  Path(sys.argv[1]).write_text(str(os.getpid()))
  os.write(2,b'x'*129)
  continue
 os.write(2,b'x'*8)
 sys.stdout.buffer.write(header+data)
 sys.stdout.buffer.flush()
"""
    session = FramedSession(
        (sys.executable, "-I", "-B", "-c", program, str(marker)), cap=128
    )
    monkeypatch.setattr(sessions, "spawn_owned_process", capture)
    try:
        first = session.ensure_generation(time.monotonic() + 5, None)
        for payload in (b"one", b"two"):
            assert (
                session.exchange(first, payload, time.monotonic() + 5, None) == payload
            )
        live = session.generation
        assert live is first and len(children) == 1
        with pytest.raises(LimitExceeded, match="diagnostics exceed protocol byte"):
            session.exchange(first, b"overflow", time.monotonic() + 5, None)
        assert marker.exists() and children[0].poll() is not None
        released = session.generation
        assert released is None
        recovered = session.ensure_generation(time.monotonic() + 5, None)
        assert recovered.number == first.number + 1
        assert recovered is not first and len(children) == 2
        assert (
            session.exchange(recovered, b"fresh", time.monotonic() + 5, None)
            == b"fresh"
        )
    finally:
        session.close()
    assert len(children) == 2 and all(child.poll() is not None for child in children)


@pytest.mark.parametrize("entry", ["generation", "frame"])
def test_known_diagnostic_overflow_rejects_reuse_before_native_input(
    tmp_path: Path, entry: str
) -> None:
    """Observed overflow discards its owner before a subsequent request is sent."""
    gate, marker = tmp_path / "emit-diagnostics", tmp_path / "processed-request"
    program = """import os,struct,sys,time
from pathlib import Path
reader=sys.stdin.buffer;writer=sys.stdout.buffer
header=reader.read(8)
data=reader.read(struct.unpack('>Q',header)[0])
writer.write(header+data);writer.flush()
while not Path(sys.argv[1]).exists():time.sleep(.001)
os.write(2,b'x'*129)
header=reader.read(8)
if header:
 data=reader.read(struct.unpack('>Q',header)[0])
 Path(sys.argv[2]).write_bytes(data)
 writer.write(header+data);writer.flush()
while True:time.sleep(1)
"""
    session = FramedSession(
        (sys.executable, "-I", "-B", "-c", program, str(gate), str(marker)), cap=128
    )
    try:
        first = session.ensure_generation(time.monotonic() + 5, None)
        assert session.exchange(first, b"first", time.monotonic() + 5, None) == b"first"
        worker = session._worker
        assert worker is not None
        gate.write_text("emit")
        assert worker.stderr_overflow.wait(5)
        with pytest.raises(LimitExceeded, match="diagnostics exceed protocol byte"):
            if entry == "generation":
                session.ensure_generation(time.monotonic() + 5, None)
            else:
                session.exchange(first, b"second", time.monotonic() + 5, None)
        assert not marker.exists() and worker.process.poll() is not None
        assert session.generation is None
        gate.unlink()
        recovered = session.ensure_generation(time.monotonic() + 5, None)
        assert recovered.number == first.number + 1
        fresh = session._worker
        assert fresh is not None and fresh.token is recovered
        assert (
            session.exchange(recovered, b"fresh", time.monotonic() + 5, None)
            == b"fresh"
        )
    finally:
        session.close()
    assert worker.process.poll() is not None and fresh.process.poll() is not None
