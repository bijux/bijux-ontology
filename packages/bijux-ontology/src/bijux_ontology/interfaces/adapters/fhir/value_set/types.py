"""Owned R5 selections and presentation occurrences with exact code identity."""

from dataclasses import dataclass

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report

from ..identity import FhirCodeIdentity


@dataclass(frozen=True)
class FhirExpansionEntry:
    """An original occurrence; navigation never establishes logical subsumption."""

    path: tuple[int, ...]
    identity: FhirCodeIdentity | None
    display: str | None
    abstract: bool
    inactive: bool | None
    metadata_json: str
    children: tuple["FhirExpansionEntry", ...]


@dataclass(frozen=True)
class FhirSelectionClause:
    """One original include/exclude, with unsupported criteria still retained."""

    system: str | None
    version: str | None
    codes: tuple[str, ...] | None
    filtered: bool
    imported: tuple[str, ...]
    metadata_json: str


@dataclass(frozen=True)
class FhirValueSetPolicy:
    """Explicit consumer treatment of unversioned identities and inactive codes.

    Allowing unversioned identities compares None with None; it does not bind a
    missing version to any available version. Abstract codes are never selectable.
    """

    allow_unversioned: bool = False
    include_inactive: bool = False


@dataclass(frozen=True, init=False)
class FhirValueSet:
    """Native-qualified source and immutable trees, without a closure assertion."""

    payload: bytes
    document_json: str
    includes: tuple[FhirSelectionClause, ...]
    excludes: tuple[FhirSelectionClause, ...]
    entries: tuple[FhirExpansionEntry, ...]
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Prevent construction without actual native source qualification."""
        raise InputError("Use read_value_set to capture an actual R5 resource")

    @classmethod
    def _owned(cls, **values: object) -> "FhirValueSet":
        """Adopt only immutable source fields already charged by the reader."""
        result = object.__new__(cls)
        for key, value in values.items():
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class FhirValueSetSelection:
    """Known selected identities beside a qualified ordinary value contract."""

    identities: tuple[FhirCodeIdentity, ...]
    excluded: tuple[FhirCodeIdentity, ...]
    enumeration: ValueEnumeration
    report: Report


@dataclass(frozen=True)
class FhirValueSetExport:
    """Actual maintained producer output, absent on incomplete qualification."""

    content: bytes | None
    report: Report
