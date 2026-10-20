"""Isolated Python entry points selected by trusted host operations."""

import sys
from collections.abc import Callable
from threading import Event

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.processes.transport import exchange_worker


def execute_worker(
    module: str,
    payload: bytes,
    *,
    cap: int,
    timeout: float | None,
    cancel: Event | None = None,
    completed: Callable[[bytes], None] | None = None,
) -> bytes:
    """Exchange opaque bytes with a trusted installed package worker.

    Callers own the entry point and its protocol. Isolated Python lookup prevents
    the working directory from replacing the installed implementation; process
    transport owns pipe bounds, cancellation and descendant cleanup.
    """
    if (
        not isinstance(module, str)
        or not module.startswith("bijux_ontology.")
        or not all(part.isidentifier() for part in module.split("."))
    ):
        raise InputError("Workers require an installed package module name")
    return exchange_worker(
        [sys.executable, "-I", "-B", "-m", module, str(cap)],
        payload,
        cap=cap,
        timeout=timeout,
        cancel=cancel,
        completed=completed,
    )
