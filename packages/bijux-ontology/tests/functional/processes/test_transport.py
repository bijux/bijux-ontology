"""Opaque request pipes preserve bytes and bound both worker output streams."""

import subprocess
import sys
import time
from collections.abc import Sequence
from threading import Event
from typing import Any, cast

import pytest
from rdflib import Graph

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.processes.transport import exchange_worker, run_worker
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
    runpy.run_module('bijux_ontology.shacl.worker', run_name='__main__')
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
                "bijux_ontology.shacl.worker",
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
