"""Bounded portable resource reads without trusting archive or filesystem paths."""

import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib.resources.abc import Traversable
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits


def resource_path(name: object) -> str:
    """Require a portable relative filename without aliases or parent traversal."""
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
    ):
        raise InputError("Bundle resources require portable relative paths")
    return name


@contextmanager
def bundle_reader(
    source: Path | Traversable, *, limits: Limits
) -> Iterator[Callable[[str], bytes]]:
    """Read owned directory, packaged-resource or ZIP members within fixed limits.

    ZIP metadata and all member paths are checked before yielding a reader.
    No archive extraction occurs here. Directory symlinks cannot escape the
    supplying tree, and every supplied file is read with an explicit size bound.
    """
    archive = None
    try:
        members = {}
        if isinstance(source, Path) and source.is_file():
            if source.stat().st_size > limits.max_total_bytes:
                raise LimitExceeded("Bundle archive exceeds max_total_bytes")
            archive = ZipFile(source)
            infos = archive.infolist()
            if len(infos) > limits.max_resources:
                raise LimitExceeded("Bundle archive exceeds max_resources")
            total = 0
            for item in infos:
                name = resource_path(
                    item.filename.rstrip("/") if item.is_dir() else item.filename
                )
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                ):
                    raise InputError(
                        "Bundle archive cannot contain links or special files"
                    )
                if item.flag_bits & 1:
                    raise InputError(
                        "Bundle archive cannot contain encrypted resources"
                    )
                if name in members:
                    raise InputError("Duplicate bundle archive member", location=name)
                members[name] = item
                if item.file_size > limits.max_bytes:
                    raise LimitExceeded(
                        "Bundle member exceeds max_bytes", location=name
                    )
                total += item.file_size
                if total > limits.max_total_bytes:
                    raise LimitExceeded("Bundle archive exceeds max_total_bytes")
            for name in members:
                parts = name.split("/")
                for index in range(1, len(parts)):
                    parent = "/".join(parts[:index])
                    if parent in members and not members[parent].is_dir():
                        raise InputError(
                            "Bundle archive file conflicts with a directory",
                            location=name,
                        )
        elif not source.is_dir():
            raise ResourceError("Bundle source must be a directory or ZIP archive")
        budget = Budget(limits)
        snapshots: dict[str, bytes] = {}

        def read(name: str) -> bytes:
            """Own one validated portable member, reading it at most once."""
            name = resource_path(name)
            if name in snapshots:
                return snapshots[name]
            budget.consume("resources")
            try:
                if archive is not None:
                    if name not in members or members[name].is_dir():
                        raise ResourceError("Missing bundle resource", location=name)
                    with archive.open(members[name]) as handle:
                        payload = handle.read(limits.max_bytes + 1)
                else:
                    member = source.joinpath(*name.split("/"))
                    if isinstance(source, Path):
                        if (
                            not source.joinpath(*name.split("/"))
                            .resolve()
                            .is_relative_to(source.resolve())
                        ):
                            raise InputError(
                                "Bundle resource escapes its directory", location=name
                            )
                        cursor = source
                        for part in name.split("/"):
                            cursor = cursor / part
                            if cursor.is_symlink():
                                raise InputError(
                                    "Bundle resources cannot be symlinks", location=name
                                )
                    if not member.is_file():
                        raise ResourceError("Missing bundle resource", location=name)
                    with member.open("rb") as handle:
                        payload = handle.read(limits.max_bytes + 1)
            except (OSError, BadZipFile, RuntimeError) as error:
                raise ResourceError(
                    "Cannot read bundle resource", location=name
                ) from error
            if len(payload) > limits.max_bytes:
                raise LimitExceeded("Bundle member exceeds max_bytes", location=name)
            budget.consume("bytes", len(payload))
            snapshots[name] = payload
            return payload

        yield read
    except (OSError, BadZipFile) as error:
        raise ResourceError("Cannot open bundle source") from error
    finally:
        if archive is not None:
            archive.close()
