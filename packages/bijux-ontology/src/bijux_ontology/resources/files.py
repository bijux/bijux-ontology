"""Bounded local file snapshots shared by owned stores and native backends."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.limits import Budget, bound_text


def _revision(value: os.stat_result) -> tuple[int, ...]:
    """Compare opened and named file identity without claiming atomic snapshots."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


@contextmanager
def capture_file(path: Path, budget: Budget) -> Iterator[tuple[BinaryIO, int]]:
    """Own one read handle and reject observed mutation or path replacement."""
    budget.consume("states", 3)
    budget.consume("bytes", 4096)
    budget.consume("work")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        yield stream, before.st_size
        budget.consume("work", 2)
        if _revision(before) != _revision(os.fstat(stream.fileno())) or (
            _revision(before) != _revision(path.stat())
        ):
            raise ResourceError("Input file changed during capture", location=str(path))


@dataclass(frozen=True)
class FileSnapshot:
    """One actual immutable file and its qualified content identity."""

    name: str
    size: int
    modified_ns: int
    inode: int
    sha256: str


def seal_files(directory: Path, budget: Budget) -> tuple[FileSnapshot, ...]:
    """Hash bounded files only after their mutable owner has closed."""
    if directory.is_symlink() or not directory.is_dir():
        raise InputError("File snapshots require an owned directory")
    result = []
    for path in directory.iterdir():
        budget.consume("work")
        budget.consume("states")
        bound_text(path.name, budget)
        if path.is_symlink() or not path.is_file():
            raise InputError("File snapshots require ordinary files")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(65536):
                budget.consume("work")
                budget.consume("bytes", len(chunk))
                digest.update(chunk)
        after = path.stat()
        if _revision(before) != _revision(after):
            raise ResourceError("Owned file changed while its seal was captured")
        result.append(
            FileSnapshot(
                path.name,
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
                digest.hexdigest(),
            )
        )
    if not result:
        raise ResourceError("Owned directory has no sealed files")
    return tuple(sorted(result, key=lambda item: item.name))


def verify_files(
    directory: Path, files: tuple[FileSnapshot, ...], budget: Budget
) -> None:
    """Reject a changed file inventory before and after every isolated read.

    Captured files are immutable. A mutable owner changes the
    directory inventory or file revision; it must never share an active reader.
    Content hashes qualify creation/restart; revision checks qualify each read.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise ResourceError("Owned snapshot directory is unavailable")
    actual = {}
    for path in directory.iterdir():
        budget.consume("work")
        budget.consume("states")
        bound_text(path.name, budget)
        if path.is_symlink() or not path.is_file():
            raise ResourceError("Owned snapshot file ownership changed")
        stat = path.stat()
        actual[path.name] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    expected = {}
    for item in files:
        budget.consume("work")
        budget.consume("states")
        bound_text(item.name, budget)
        expected[item.name] = (item.size, item.modified_ns, item.inode)
    if actual != expected:
        raise ResourceError("Owned snapshot changed after publication")
