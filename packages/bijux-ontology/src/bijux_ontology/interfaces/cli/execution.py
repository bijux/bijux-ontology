"""A real owned subprocess deadline for expensive console checks."""

import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.cli.checkpoints import read_completed
from bijux_ontology.interfaces.cli.protocol import report_from_data
from bijux_ontology.interfaces.cli.results import CommandResult
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.workflows.projects.configuration import ProjectConfig


def _stop(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()


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
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bijux_ontology.interfaces.cli.worker",
                str(request),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
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
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stopped = "execution.deadline"
        except KeyboardInterrupt:
            stopped = "execution.cancelled"
        finally:
            _stop(process)
            reader.join(timeout=1)
            if process.stderr is not None and not reader.is_alive():
                process.stderr.close()
        if stopped or (work / "result-exhausted").exists():
            reports = list(read_completed(work, limits))
            reports.append(
                Report(
                    "execution",
                    requested=1,
                    issues=(
                        Issue(
                            stopped or "execution.limit_exceeded",
                            "Owned CLI execution stopped before checking completed"
                            if stopped
                            else "Complete command result exceeded max_total_bytes",
                            kind="exhausted",
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
