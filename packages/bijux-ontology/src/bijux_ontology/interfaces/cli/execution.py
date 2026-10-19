"""A real owned subprocess deadline for expensive console checks."""

import json
import math
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.diagnostics.reports import (
    Issue,
    Report,
    combine_reports,
    report_from_data,
)
from bijux_ontology.interfaces.cli.results import CommandResult
from bijux_ontology.interfaces.execution.checkpoints import read_completed
from bijux_ontology.limits import Limits
from bijux_ontology.processes.ownership import spawn_owned_process
from bijux_ontology.processes.transport import stop_process
from bijux_ontology.workflows.projects.configuration import ProjectConfig


def execute_bounded(
    argv: list[str],
    *,
    timeout: float,
    limits: Limits,
    configuration: ProjectConfig | None = None,
) -> CommandResult:
    """Interrupt the worker, retaining completed project diagnostics on timeout.

    The synchronous API deliberately has no hard CPU-time promise. POSIX worker
    groups include owned shape workers; other platforms terminate the direct
    worker. Temporary command protocol files live below the caller's artifacts.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise InputError("timeout must be a positive finite number")
    root = Path.cwd() / "artifacts" / "ontology-execution"
    root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=root) as directory:
        work = Path(directory)
        request = work / "request.json"
        request.write_text(
            json.dumps(
                {
                    "argv": argv,
                    "project": {
                        "data": configuration.to_dict(),
                        "directory": str(configuration.directory),
                        "source": configuration.source,
                    }
                    if configuration is not None
                    else None,
                }
            ),
            encoding="utf-8",
        )
        process = spawn_owned_process(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "bijux_ontology.interfaces.cli.worker",
                str(request),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        error_output = bytearray()

        def drain_errors() -> None:
            """Drain output while retaining a bounded error excerpt."""
            assert process.stderr is not None
            while chunk := process.stderr.read(4096):
                remaining = 65_536 - len(error_output)
                if remaining > 0:
                    error_output.extend(chunk[:remaining])

        reader = Thread(target=drain_errors, daemon=True)
        reader.start()
        stopped = ""
        failure: BaseException | None = None
        cleanup_error: ResourceError | None = None
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped = "execution.deadline"
        except KeyboardInterrupt:
            stopped = "execution.cancelled"
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                stop_process(process, failure=failure)
            except ResourceError as error:
                cleanup_error = error
            finally:
                reader.join(timeout=1)
                if process.stderr is not None and not reader.is_alive():
                    process.stderr.close()
        if stopped or cleanup_error is not None or (work / "result-exhausted").exists():
            reports = list(read_completed(work, limits))
            reports.append(
                Report(
                    "execution",
                    requested=1,
                    issues=(
                        Issue(
                            stopped
                            or (
                                "execution.worker_failed"
                                if cleanup_error is not None
                                else "execution.limit_exceeded"
                            ),
                            (
                                "Owned CLI execution stopped before checking completed"
                                if stopped
                                else "Owned CLI worker cleanup failed"
                                if cleanup_error is not None
                                else "Complete command result exceeded max_total_bytes"
                            )
                            + (
                                f"; {cleanup_error}"
                                if cleanup_error is not None
                                else ""
                            ),
                            kind="input_error"
                            if cleanup_error is not None and not stopped
                            else "exhausted",
                        ),
                    ),
                )
            )
            return CommandResult(
                combine_reports("execution", *reports), output_safe=False
            )
        if process.returncode != 0:
            raise RuntimeError(
                "Ontology check worker failed:\n"
                + error_output.decode("utf-8", errors="replace")
            )
        with (work / "result.json").open("rb") as handle:
            payload = handle.read(limits.max_total_bytes + 1)
        response = object_value(
            decode_json(payload, max_bytes=limits.max_total_bytes),
            location="worker result",
        )
        return CommandResult(
            report_from_data(response.get("report")),
            response.get("data"),
            output_safe=response.get("output_safe") is True,
        )
