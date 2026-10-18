"""Installed bounded worker entry point for explicit embedding requests."""

import json
import sys
from pathlib import Path
from typing import cast

from bijux_ontology._json import decode_json, object_value, string_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.execution.checkpoints import (
    retain_completed,
    retain_deadline,
)
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Issue, Location, Report


def execute(path: Path, cap: int) -> None:
    """Return structured truth, keeping completed project scopes before final output."""
    with path.open("rb") as handle:
        payload = handle.read(cap + 1)
    request = object_value(
        decode_json(payload, max_bytes=cap),
        location="worker request",
    )
    if type(request.get("protocol")) is not int or request["protocol"] != 1:
        raise InputError("Unsupported execution protocol")
    supplied_limits = object_value(request.get("limits"), location="worker limits")
    if any(type(value) is not int for value in supplied_limits.values()):
        raise InputError("Worker limit values must be integers")
    limits = Limits(**cast(dict[str, int], supplied_limits))
    try:
        memory = request.get("memory_bytes")
        if memory is not None:
            if type(memory) is not int or memory <= 0:
                raise InputError("memory_bytes must be a positive integer")
            try:
                import resource

                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            except (ImportError, AttributeError, OSError, ValueError) as error:
                raise InputError(
                    "Address-space memory limits unavailable on this platform"
                ) from error
        from bijux_ontology.interfaces.execution.operations import execute_operation

        def completed(report: Report) -> None:
            """Persist only intact bounded completed-scope evidence."""
            retain_completed(report, path.parent, max_bytes=limits.max_total_bytes)

        report, data = execute_operation(
            string_value(request.get("operation")),
            object_value(
                request.get("parameters"),
                location="worker request",
            ),
            limits,
            completed,
            job_deadline=lambda value: retain_deadline(value, path.parent),
        )
    except (InputError, OSError, MemoryError) as error:
        exhausted = isinstance(error, (LimitExceeded, MemoryError))
        report = Report(
            "execution",
            requested=1,
            complete=False,
            issues=(
                Issue(
                    "execution.limit_exceeded" if exhausted else "input.invalid",
                    str(error) or "Worker exhausted memory",
                    details=Location.from_error(error),
                    kind="exhausted" if exhausted else "input_error",
                ),
            ),
        )
        data = None
    encoded = json.dumps(
        {"protocol": 1, "report": report.to_dict(), "data": data}, ensure_ascii=False
    ).encode("utf-8")
    if len(encoded) > limits.max_total_bytes:
        (path.parent / "result-exhausted").touch()
    else:
        (path.parent / "result.json").write_bytes(encoded)


if __name__ == "__main__":
    execute(Path(sys.argv[1]), int(sys.argv[2]))
