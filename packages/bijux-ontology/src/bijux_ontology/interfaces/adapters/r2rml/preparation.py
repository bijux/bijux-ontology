"""Owned local snapshots and actual optional Morph-KGC capability preparation."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    Limits,
    bound_text,
)

from .materialization import _call
from .types import (
    MorphBackend,
    SqliteSnapshot,
)


def _owned(cls: type[MorphBackend] | type[SqliteSnapshot], **values: object) -> object:
    """Publish private factories only after actual input/capability qualification."""
    result = object.__new__(cls)
    for name, value in values.items():
        object.__setattr__(result, name, value)
    return result


def prepare_morph_backend(
    python: Path,
    *,
    artifacts: Path | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> MorphBackend:
    """Qualify a separate normal Morph-KGC install without changing dependencies.

    Morph-KGC's RDFLib/PyOxigraph requirements conflict with the core pins.
    Its explicitly supplied interpreter runs a fixed concrete isolated worker;
    no environment is discovered, installed or added to Python's source path.
    """
    operation = Operation(limits, budget, timeout, None)
    if type(python) is not type(Path()) or (
        artifacts is not None and not isinstance(artifacts, Path)
    ):
        raise InputError("Native interpreter and artifact destination require Paths")
    destination = (
        artifacts
        if artifacts is not None
        else Path.cwd() / "artifacts" / "relational-rdf"
    )
    bound_text(str(destination), operation)
    if any(value in str(destination) for value in ("\r", "\n", ",", "%", "${")):
        raise InputError("Artifact paths cannot contain native config delimiters")
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="capability-", dir=destination) as job:
        _call(python, {"action": "capability"}, Path(job), operation, file_cap=16384)
    return cast(
        MorphBackend,
        _owned(
            MorphBackend,
            python=python,
            artifacts=destination.absolute(),
            version="2.10.0",
        ),
    )


def _read_file(path: Path, operation: Budget) -> bytes:
    """Pay the complete finite allocation before an owned binary read."""
    size = path.stat().st_size
    if size > operation.limits.max_bytes:
        raise LimitExceeded("SQLite snapshot exceeds max_bytes")
    operation.consume("bytes", size + 1)
    operation.consume("work", size + 1)
    with path.open("rb") as reader:
        raw = reader.read(size + 1)
    if len(raw) != size:
        raise ResourceError("SQLite snapshot changed during capture")
    return raw


def prepare_sqlite_snapshot(
    path: Path,
    *,
    artifacts: Path | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
) -> SqliteSnapshot:
    """Capture a read-only consistent SQLite backup and preserve original integrity.

    This local snapshot profile excludes adjacent WAL/SHM resources. SQLite's
    maintained backup operation supplies the consistent owned database image;
    no live connection or mapping SQL is accepted. Source changes invalidate
    capture. The materializer sees only a subsequent owned read-only copy.
    """
    operation = Operation(limits, budget, timeout, None)
    if not isinstance(path, Path):
        raise InputError("SQLite snapshots require an explicit local Path")
    bound_text(str(path), operation)
    if (
        path.is_symlink()
        or not path.is_file()
        or any(
            Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        )
    ):
        raise ResourceError("Supply one ordinary quiescent local SQLite snapshot")
    original = _read_file(path, operation)
    if not original.startswith(b"SQLite format 3\x00"):
        raise InputError("The supplied snapshot is not a SQLite database")
    digest = hashlib.sha256(original).hexdigest()
    destination = (
        artifacts
        if artifacts is not None
        else Path.cwd() / "artifacts" / "relational-rdf"
    )
    bound_text(str(destination), operation)
    destination.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="sqlite-snapshot-", dir=destination) as job:
        output = Path(job) / "snapshot.sqlite"
        try:
            with (
                sqlite3.connect(
                    path.absolute().as_uri() + "?mode=ro", uri=True, timeout=0
                ) as source,
                sqlite3.connect(output) as target,
            ):

                def progress(status: int, remaining: int, total: int) -> None:
                    """Bound maintained backup callbacks, including retries."""
                    operation.consume("work")
                    if total * page_size > operation.limits.max_bytes:
                        raise LimitExceeded(
                            "SQLite backup exceeds its finite page allowance"
                        )

                page_size = source.execute("PRAGMA page_size").fetchone()[0]
                source.backup(target, pages=16, progress=progress, sleep=0.001)
        except sqlite3.Error as error:
            raise ResourceError(
                "The read-only SQLite snapshot is unavailable"
            ) from error
        content = _read_file(output, operation)
    if hashlib.sha256(_read_file(path, operation)).hexdigest() != digest:
        raise ResourceError("The original SQLite snapshot changed during capture")
    operation.consume("states")
    return cast(
        SqliteSnapshot,
        _owned(
            SqliteSnapshot,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            source_sha256=digest,
            source_name=str(path),
        ),
    )
