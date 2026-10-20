"""Explicit retained source associations and factory-owned checked patch output."""

import json
from dataclasses import dataclass
from typing import Any

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.documents.forms.types import DocumentFormCheck
from bijux_ontology.json.paths import SourcePath, validate_path


@dataclass(frozen=True)
class RetainedUsage:
    """Bind an unchanged field to explicit before/after concrete source paths.

    Changed paths require the same established named parent. Anonymous relocation
    is deliberately unqualified; array positions never establish correspondence.
    The selected raw value must stay identical as well as its expanded meaning.
    """

    before_path: SourcePath
    after_path: SourcePath

    def __post_init__(self) -> None:
        for path in (self.before_path, self.after_path):
            validate_path(path)
            if not path:
                raise InputError("Retained usages select nonempty field paths")


@dataclass(frozen=True, init=False)
class DocumentPatch:
    """Immutable candidate bytes are available only after every required check.

    Construction belongs to apply_document_patch. document returns an independent
    ordinary JSON copy. Raw form/source and retention reports remain available
    after rejection; no partially edited representation authorizes export.
    """

    document_json: str | None
    report: Report
    before_form: DocumentFormCheck | None
    after_form: DocumentFormCheck | None
    retained_report: Report | None
    operations_checked: int

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Reject detached manual construction and dataclass replacement."""
        raise InputError("Document patches belong to apply_document_patch")

    @classmethod
    def _from_owned(cls, **values: Any) -> "DocumentPatch":
        """Adopt the operation's qualified bytes and already retained evidence."""
        result = object.__new__(cls)
        for key in cls.__dataclass_fields__:
            object.__setattr__(result, key, values[key])
        return result

    @property
    def document(self) -> object | None:
        """Read a fresh copy without changing the checked source representation."""
        return (
            json.loads(self.document_json) if self.document_json is not None else None
        )
