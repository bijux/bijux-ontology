"""Bounded local file snapshots shared by owned stores and native backends."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.execution.limits import Budget, bound_text


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
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
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
