"""Explicit versioned resources, characteristic policies and retained occurrences."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.interfaces.adapters.vocabulary_bindings.controlled.types import (
    AllowedExpression,
)


@dataclass(frozen=True)
class SchemaResource:
    """Caller-supplied immutable schema bytes with a declared version and base URI."""

    uri: str
    version: str
    content: bytes

    def __post_init__(self) -> None:
        """Require explicit identities without reading or acquiring a resource."""
        if (
            type(self.uri) is not str
            or not self.uri
            or type(self.version) is not str
            or not self.version
            or type(self.content) is not bytes
        ):
            raise InputError(
                "Schema resources require URI, version and immutable bytes"
            )


@dataclass(frozen=True)
class CharacteristicBinding:
    """Select an exact attribute and its explicit term/text/unit obligations.

    Text and unit enumerations contain RDF literals. Ontology enumerations contain
    named IRIs; declaration kinds are explicit rather than inferred from prose.
    """

    attribute: str
    terms: AllowedExpression | None = None
    texts: AllowedExpression | None = None
    units: AllowedExpression | None = None
    kinds: tuple[str, ...] = ()
    require_terms: bool = True
    require_text: bool = False
    require_unit: bool = False

    def __post_init__(self) -> None:
        """Require an immutable named selection and explicit boolean requirements."""
        if (
            type(self.attribute) is not str
            or not self.attribute
            or type(self.kinds) is not tuple
            or any(type(kind) is not str for kind in self.kinds)
            or any(
                type(value) is not bool
                for value in (self.require_terms, self.require_text, self.require_unit)
            )
        ):
            raise InputError(
                "Characteristic bindings need named, typed finite policies"
            )


@dataclass(frozen=True)
class CharacteristicOccurrence:
    """One actual array position, including repeated values and original strings."""

    attribute: str
    index: int
    location: str
    text: str | None
    unit: str | None
    ontology_terms: tuple[str, ...]
    tag: str | None = None


@dataclass(frozen=True)
class SampleCheck:
    """Owned sample bytes and qualified resource identities with raw child reports."""

    sample_json: str | None
    occurrences: tuple[CharacteristicOccurrence, ...]
    resources: tuple[tuple[str, str, str], ...]
    structural_reports: tuple[Report, ...]
    semantic_reports: tuple[Report, ...]
    report: Report
