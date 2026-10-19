"""Sealed maintained ROBOT runtime ownership and native execution receipts."""

from dataclasses import dataclass
from pathlib import Path

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.stores.seals import StoreFile


@dataclass(frozen=True, init=False)
class RobotBackend:
    """One caller-pinned standalone ROBOT release, with no ambient installation."""

    java: Path
    jar: Path
    seal: StoreFile
    artifacts: Path

    def __init__(self) -> None:
        """Require qualification of the actual maintained runtime file."""
        raise InputError("Use prepare_robot_backend")

    @classmethod
    def _owned(cls, **values: object) -> "RobotBackend":
        """Adopt only bounded explicitly qualified immutable runtime metadata."""
        result = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class RobotRun:
    """Actual native identity, diagnostics and bounded owned output bytes."""

    identity: str
    returncode: int | None
    diagnostic: str
    outputs: tuple[bytes | None, ...]
