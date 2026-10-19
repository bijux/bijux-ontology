"""Atomic publication of owned bytes to an explicit caller-selected file."""

import os
import stat
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from bijux_ontology.errors import InputError, ResourceError


def write_bytes(content: bytes, destination: Path, *, overwrite: bool = False) -> None:
    """Publish complete bytes without exposing a partially written destination."""
    if not isinstance(content, bytes) or not isinstance(destination, Path):
        raise InputError("Document publication requires bytes and a chosen path")
    if type(overwrite) is not bool:
        raise InputError("overwrite must be a boolean")
    private: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix="." + destination.name + ".",
            suffix=".part",
            delete=False,
        ) as stream:
            private = Path(stream.name)
            if overwrite:
                try:
                    existing = destination.stat(follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if stat.S_ISREG(existing.st_mode):
                        os.chmod(private, stat.S_IMODE(existing.st_mode))
            if stream.write(content) != len(content):
                raise ResourceError("The output file rejected the complete export")
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(private, destination)
        else:
            os.link(private, destination)
    except OSError as error:
        raise ResourceError(f"Cannot publish document: {error}") from error
    finally:
        if private is not None:
            with suppress(OSError):
                private.unlink(missing_ok=True)
