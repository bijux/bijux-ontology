"""Factory-owned relocation candidates bind exact bytes to required evidence."""

import json
from dataclasses import dataclass
from typing import Any

from bijux_ontology.documents.forms.types import DocumentFormCheck
from bijux_ontology.errors import InputError
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Report


@dataclass(frozen=True, init=False)
class DocumentRelocation:
    """An explicit preservation candidate, not a migration to new identities.

    Construction belongs to relocate_document. document returns independent
    ordinary JSON and document_json is immutable; neither changes the checked
    representation. No candidate bytes are published on failure or unknown.
    Raw before/after reports and available sources remain inspectable.
    """

    old_url: str | None
    new_url: str | None
    document_json: str | None
    report: Report
    before_form: DocumentFormCheck | None
    after_form: DocumentFormCheck | None
    before_source: PreparedSource | None
    after_source: PreparedSource | None
    usage_report: Report | None
    rdf_report: Report | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Reject detached manual construction and dataclass replacement."""
        raise InputError("Relocation candidates belong to relocate_document")

    @classmethod
    def _from_owned(cls, **values: Any) -> "DocumentRelocation":
        """Adopt operation-qualified immutable bytes and their actual reports."""
        result = object.__new__(cls)
        for key in cls.__dataclass_fields__:
            object.__setattr__(result, key, values[key])
        return result

    @property
    def document(self) -> object | None:
        """Return an independent copy of the exact qualified candidate values."""
        return (
            json.loads(self.document_json) if self.document_json is not None else None
        )
