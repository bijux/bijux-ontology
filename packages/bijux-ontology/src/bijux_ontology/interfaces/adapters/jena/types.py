"""An explicitly pinned local Jena runtime, independent of optional OWL engines."""

from dataclasses import dataclass
from pathlib import Path

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.stores.seals import StoreFile


@dataclass(frozen=True, init=False)
class JenaBackend:
    """Factory-owned exact classpath and immutable file revisions."""

    java: Path
    directory: Path
    jars: tuple[str, ...]
    files: tuple[StoreFile, ...]
    artifacts: Path
    manifest_sha256: str

    def __init__(self) -> None:
        """Reject unqualified direct construction of a native runtime."""
        raise InputError("Use prepare_jena_backend with an explicit local bundle")

    @classmethod
    def _owned(cls, **values: object) -> "JenaBackend":
        """Adopt only fields qualified by the local artifact factory."""
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result
