"""Detect changes to a closed owned database before adopting read-only answers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import bound_text


@dataclass(frozen=True)
class StoreFile:
    """One actual immutable database file and its qualified content identity."""

    name: str
    size: int
    modified_ns: int
    inode: int
    sha256: str


def seal_files(directory: Path, budget: Budget) -> tuple[StoreFile, ...]:
    """Hash bounded files only after the native mutable owner has closed."""
    if directory.is_symlink() or not directory.is_dir():
        raise InputError("Stored RDF requires an owned database directory")
    result = []
    for path in directory.iterdir():
        budget.consume("work")
        budget.consume("states")
        bound_text(path.name, budget)
        if path.is_symlink() or not path.is_file():
            raise InputError("Stored RDF snapshots require ordinary database files")
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
            raise ResourceError("Stored RDF changed while its seal was captured")
        result.append(
            StoreFile(
                path.name,
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
                digest.hexdigest(),
            )
        )
    if not result:
        raise ResourceError("Stored RDF database has no sealed files")
    return tuple(sorted(result, key=lambda item: item.name))


def verify_files(directory: Path, files: tuple[StoreFile, ...], budget: Budget) -> None:
    """Reject a changed file inventory before and after every isolated read.

    Native backup files are immutable. A mutable native opener changes the
    directory inventory or file revision; it must never share an active reader.
    Content hashes qualify creation/restart; revision checks qualify each read.
    """
    if directory.is_symlink() or not directory.is_dir():
        raise ResourceError("Stored RDF snapshot directory is unavailable")
    actual = {}
    for path in directory.iterdir():
        budget.consume("work")
        budget.consume("states")
        bound_text(path.name, budget)
        if path.is_symlink() or not path.is_file():
            raise ResourceError("Stored RDF snapshot file ownership changed")
        stat = path.stat()
        actual[path.name] = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    expected = {}
    for item in files:
        budget.consume("work")
        budget.consume("states")
        bound_text(item.name, budget)
        expected[item.name] = (item.size, item.modified_ns, item.inode)
    if actual != expected:
        raise ResourceError("Stored RDF snapshot changed after publication")
