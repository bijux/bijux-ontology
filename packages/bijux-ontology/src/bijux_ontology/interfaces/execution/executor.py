"""Explicit bounded execution in a caller-supplied installed interpreter."""

import json
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from bijux_ontology._json import decode_json, object_value, own_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import (
    Issue,
    Report,
    combine_reports,
    report_from_data,
)
from bijux_ontology.execution.limits import Limits
from bijux_ontology.execution.processes.transport import run_worker
from bijux_ontology.interfaces.execution.checkpoints import (
    read_completed,
    read_deadline,
)


@dataclass(frozen=True)
class ExecutionResult:
    """Raw report and separately owned JSON output; failures never imply success."""

    report: Report
    data: object = None


@dataclass(frozen=True)
class WorkerExecutor:
    """Run finite operations without installing or altering any host dependencies.

    python names a trusted, already installed runtime. JSON resources replace
    callbacks. Deadlines cover startup and computation; POSIX cancellation kills
    owned process groups, other platforms terminate the direct worker. Optional
    memory_bytes requests the platform address-space limit and fails if unavailable.
    """

    python: str | Path = sys.executable
    timeout: float = 30.0
    limits: Limits = field(default_factory=Limits)
    memory_bytes: int | None = None
    artifacts: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.python, (str, Path)) or not str(self.python):
            raise InputError("A worker requires an installed Python interpreter")
        if (
            type(self.timeout) not in (int, float)
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise InputError("timeout must be finite and positive")
        if not isinstance(self.limits, Limits):
            raise InputError("Worker limits require Limits")
        if self.memory_bytes is not None and (
            type(self.memory_bytes) is not int or self.memory_bytes <= 0
        ):
            raise InputError("memory_bytes must be a positive integer")

    def run(
        self,
        operation: str,
        parameters: Mapping[str, object],
        *,
        cancellation: Event | None = None,
    ) -> ExecutionResult:
        """Execute supported JSON-LD, vocabulary and project contracts.

        Parameters are JSON-shaped public inputs; there is no function, module or
        pickle dispatch. Returned reports reconstruct raw truth rather than trust
        worker-supplied policy or exit status. Completed project findings survive
        interruption when checkpoint evidence was produced within the output cap.
        """
        if cancellation is not None and not isinstance(cancellation, Event):
            raise InputError("cancellation requires a threading.Event")
        root = self.artifacts or Path.cwd() / "artifacts" / "ontology-execution"
        root.mkdir(parents=True, exist_ok=True)
        reports: list[Report] = []
        try:
            envelope = own_json(
                {
                    "protocol": 1,
                    "operation": operation,
                    "parameters": dict(parameters),
                    "limits": vars(self.limits),
                    "memory_bytes": self.memory_bytes,
                },
                max_bytes=self.limits.max_total_bytes,
                max_depth=self.limits.max_depth,
            )
            encoded = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            if len(encoded) > self.limits.max_total_bytes:
                raise LimitExceeded("Worker request exceeds max_total_bytes")
            with TemporaryDirectory(dir=root) as directory:
                work = Path(directory)
                request = work / "request.json"
                request.write_bytes(encoded)
                if cancellation is not None and cancellation.is_set():
                    return self._failure(
                        "execution.cancelled",
                        "Execution cancelled before startup",
                        reports,
                    )
                try:
                    code, stopped, detail = run_worker(
                        [
                            str(self.python),
                            "-I",
                            "-B",
                            "-m",
                            "bijux_ontology.interfaces.execution.worker",
                            str(request.resolve()),
                            str(self.limits.max_total_bytes),
                        ],
                        timeout=self.timeout,
                        cap=self.limits.max_total_bytes,
                        cancellation=cancellation,
                        deadline=(lambda: read_deadline(work))
                        if operation == "project"
                        else None,
                    )
                except ResourceError:
                    reports.extend(read_completed(work, self.limits))
                    raise
                reports.extend(read_completed(work, self.limits))
                if stopped or (work / "result-exhausted").exists():
                    return self._failure(
                        stopped or "execution.limit_exceeded",
                        "Owned worker stopped before returning its complete result"
                        + (f": {detail[:2000]}" if detail else ""),
                        reports,
                    )
                if code:
                    return self._failure(
                        "execution.worker_failed",
                        f"Worker exited {code}: {detail[:2000]}",
                        reports,
                    )
                with (work / "result.json").open("rb") as handle:
                    raw = handle.read(self.limits.max_total_bytes + 1)
                response = object_value(
                    decode_json(
                        raw,
                        max_bytes=self.limits.max_total_bytes,
                        max_depth=self.limits.max_depth,
                    ),
                    location="worker request",
                )
                if (
                    type(response.get("protocol")) is not int
                    or response["protocol"] != 1
                ):
                    raise InputError("Unsupported worker protocol")
                return ExecutionResult(
                    report_from_data(response.get("report")), response.get("data")
                )
        except LimitExceeded as error:
            return self._failure("execution.limit_exceeded", str(error), reports)
        except (InputError, OSError, TypeError, ValueError) as error:
            return self._failure("execution.worker_failed", str(error), reports)

    @staticmethod
    def _failure(code: str, message: str, reports: list[Report]) -> ExecutionResult:
        """Retain earlier findings alongside an explicit incomplete final scope."""
        issue = Issue(
            code,
            message,
            kind="exhausted"
            if code
            in {"execution.deadline", "execution.cancelled", "execution.limit_exceeded"}
            else "input_error",
        )
        return ExecutionResult(
            combine_reports(
                "execution",
                *reports,
                Report("execution", requested=1, complete=False, issues=(issue,)),
            )
        )
