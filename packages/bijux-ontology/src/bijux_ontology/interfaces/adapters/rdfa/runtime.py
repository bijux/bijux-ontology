"""Explicit RDFa npm asset preparation without installation or global changes."""

from importlib.resources import files
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError, ResourceError


def prepare_rdfa_runtime(directory: Path) -> tuple[Path, Path]:
    """Copy shipped npm inputs; the caller runs ordinary npm ci in this directory.

    Existing differing files are rejected. This helper neither installs software
    nor claims that arbitrary existing node_modules match the supplied lock.
    """
    if not isinstance(directory, Path):
        raise InputError("RDFa runtime preparation requires a Path")
    resources = files(__package__).joinpath("node")
    captured: list[tuple[Path, bytes]] = []
    try:
        for name in ("package.json", "package-lock.json"):
            resource = resources.joinpath(name)
            with resource.open("rb") as stream:
                data = stream.read(1_000_001)
            if len(data) > 1_000_000:
                raise ResourceError("Packaged RDFa npm assets exceed their bound")
            target = directory / name
            if target.exists():
                with target.open("rb") as stream:
                    previous = stream.read(len(data) + 1)
                if previous != data:
                    raise InputError("RDFa runtime already has different npm inputs")
            captured.append((target, data))
        directory.mkdir(parents=True, exist_ok=True)
        for target, data in captured:
            if not target.exists():
                with target.open("xb") as stream:
                    stream.write(data)
    except OSError as error:
        raise ResourceError(
            "Could not prepare the explicit RDFa runtime directory"
        ) from error
    return captured[0][0], captured[1][0]
