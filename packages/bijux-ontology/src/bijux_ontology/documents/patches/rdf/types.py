"""Explicit delta pairing, source occurrence policy and accepted projection output."""

import json
from dataclasses import dataclass
from typing import Literal

from bijux_ontology._jsonpaths import SourcePath, validate_path
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.documents.patches.types import DocumentPatch


@dataclass(frozen=True)
class SourceEditBinding:
    """Pair exact delta indices; choose one unique, all, or explicit occurrences."""

    removal: int
    addition: int
    paths: tuple[SourcePath, ...] | Literal["all"] | None = None

    def __post_init__(self) -> None:
        """Require concrete immutable paths; source ownership is established later."""
        if any(
            type(index) is not int or index < 0
            for index in (self.removal, self.addition)
        ):
            raise InputError("Source edits require nonnegative delta indices")
        if self.paths is not None and self.paths != "all":
            if not isinstance(self.paths, tuple) or not self.paths:
                raise InputError("Explicit source edit paths require a nonempty tuple")
            for path in self.paths:
                validate_path(path)
                if not path:
                    raise InputError("Source edits select field occurrences")


@dataclass(frozen=True, init=False)
class RDFSourcePatch:
    """Only whole semantic agreement publishes a checked source patch or operations."""

    report: Report
    patch: DocumentPatch | None
    operations_json: str | None
    bindings: tuple[SourceEditBinding, ...]

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Reject detached construction and dataclass replacement of checked output."""
        raise InputError("RDF source patches belong to project_rdf_patch")

    @classmethod
    def _from_owned(
        cls,
        report: Report,
        patch: DocumentPatch | None,
        operations_json: str | None,
        bindings: tuple[SourceEditBinding, ...],
    ) -> "RDFSourcePatch":
        """Attach only this operation's immutable bytes and qualified evidence."""
        result = object.__new__(cls)
        object.__setattr__(result, "report", report)
        object.__setattr__(result, "patch", patch)
        object.__setattr__(result, "operations_json", operations_json)
        object.__setattr__(result, "bindings", bindings)
        return result

    @property
    def operations(self) -> object | None:
        """Return independent ordinary JSON without exposing mutable accepted state."""
        return (
            json.loads(self.operations_json)
            if self.operations_json is not None
            else None
        )
