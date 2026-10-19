"""Opaque request pipes preserve bytes and bound both worker output streams."""

import sys

import pytest

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.processes.transport import exchange_worker


def test_binary_exchange_bounds_stdout_and_stderr_and_recovers() -> None:
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
    assert exchange_worker(echo, payload, cap=128, timeout=3) == payload
    for stream in ("stdout", "stderr"):
        overflow = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            f"import sys; sys.{stream}.write('x'*1000)",
        ]
        with pytest.raises(LimitExceeded, match="output exceeds"):
            exchange_worker(overflow, b"", cap=32, timeout=3)
    assert exchange_worker(echo, payload, cap=128, timeout=3) == payload
