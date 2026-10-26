"""Explicit admission for a separately installed compatible Frictionless runtime."""

import math
import threading
from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits, bound_text

from .extraction import _run
from .identity import worker_identity
from .profile import object_value, text
from .types import FrictionlessBackend


def prepare_frictionless_backend(
    python: Path,
    *,
    work_directory: Path,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> FrictionlessBackend:
    """Check an explicit interpreter with this installed worker and native 5.21.0.

    Both interpreters need the same bijux-ontology distribution. This operation
    neither discovers environments nor installs dependencies. Native imports run
    under the same reaped process deadline as extraction; no tables are read.
    """
    if (
        not isinstance(python, Path)
        or not python.is_absolute()
        or not python.is_file()
        or not isinstance(work_directory, Path)
        or not work_directory.is_dir()
        or type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise InputError(
            "Supply an interpreter, existing work directory and positive timeout"
        )
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise InputError("Cancellation requires threading.Event")
    current = Operation(limits, budget, timeout, cancel)
    bound_text(str(python), current)
    bound_text(str(work_directory), current)
    expected = worker_identity(current)
    with TemporaryDirectory(dir=work_directory, prefix="frictionless-") as directory:
        response = _run(
            {},
            directory,
            current,
            python=python,
            expected_worker=expected,
            capability=True,
        )
    if response["status"] == "exhausted":
        raise LimitExceeded("Native package capability exhausted its allowance")
    if response["status"] != "completed" or response["complete"] is not True:
        raise ResourceError(
            text(response["message"]) or "Native package capability unavailable"
        )
    producer = object_value(response["producer"])
    if (
        set(producer) != {"framework", "python", "standard"}
        or producer["framework"] != "5.21.0"
        or producer["standard"] != "v1"
    ):
        raise ResourceError("Native package capability identity is not qualified")
    observed = text(producer["python"])
    bound_text(observed, current)
    return FrictionlessBackend._admitted(python, observed, expected["sha256"])
