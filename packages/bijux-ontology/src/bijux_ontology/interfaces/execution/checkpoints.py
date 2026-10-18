"""Bounded completed-scope evidence for owned execution interruptions."""

import json
import math
import os
import stat
from pathlib import Path

from bijux_ontology._json import decode_json
from bijux_ontology.errors import ResourceError
from bijux_ontology.interfaces.execution.protocol import report_from_data
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Issue, Report


def retain_completed(report: Report, directory: Path, *, max_bytes: int) -> None:
    """Keep a complete prefix of reports, never a partial JSON record."""
    marker = directory / "checkpoint-exhausted"
    if marker.exists():
        return
    path = directory / "completed.jsonl"
    encoded = (json.dumps(report.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
    previous = path.stat().st_size if path.exists() else 0
    if previous + len(encoded) > max_bytes:
        marker.touch()
        return
    with path.open("ab") as handle:
        handle.write(encoded)


def read_completed(directory: Path, limits: Limits) -> tuple[Report, ...]:
    """Retain intact earlier evidence even if a later record exceeds the cap."""
    path = directory / "completed.jsonl"
    payload = b""
    if path.exists():
        with path.open("rb") as handle:
            payload = handle.read(limits.max_total_bytes + 1)
    reports = []
    consumed = 0
    for line in payload.splitlines(keepends=True):
        consumed += len(line)
        if consumed > limits.max_total_bytes or not line.endswith(b"\n"):
            break
        reports.append(
            report_from_data(decode_json(line, max_bytes=limits.max_total_bytes))
        )
    if (
        len(payload) > limits.max_total_bytes
        or (directory / "checkpoint-exhausted").exists()
    ):
        reports.append(
            Report(
                "execution.evidence",
                requested=1,
                issues=(
                    Issue(
                        "execution.limit_exceeded",
                        "Completed diagnostic evidence exceeded max_total_bytes",
                        kind="exhausted",
                    ),
                ),
            )
        )
    return tuple(reports)


def retain_deadline(value: float | None, directory: Path) -> None:
    """Publish one currently owned job deadline atomically for its supervisor."""
    path = directory / "job-deadline"
    if value is None:
        path.unlink(missing_ok=True)
        return
    writing = directory / "job-deadline.write"
    writing.write_text(str(value), encoding="ascii")
    writing.replace(path)


def read_deadline(directory: Path) -> float | None:
    """Read a bounded regular deadline file without blocking on a bad descriptor."""
    try:
        descriptor = os.open(
            directory / "job-deadline",
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0),
        )
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64:
            raise ResourceError("Worker job deadline is not a bounded regular file")
        raw = os.read(descriptor, 65)
    finally:
        os.close(descriptor)
    try:
        value = float(raw.decode("ascii"))
    except (ValueError, UnicodeError) as error:
        raise ResourceError("Worker job deadline is invalid") from error
    if len(raw) > 64 or not math.isfinite(value) or value <= 0:
        raise ResourceError("Worker job deadline is invalid")
    return value
