"""Plain declarations supplied by application-owned extraction adapters."""

from collections.abc import Iterable
from dataclasses import KW_ONLY, dataclass
from typing import Literal, TypeVar

from rdflib import RDF
from rdflib import Literal as RDFLiteral

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag

ValueKind = Literal["literal", "iri", "object", "unknown"]
Emission = Literal["unknown", "optional", "guaranteed"]


_T = TypeVar("_T")


def _sequence(value: Iterable[_T], location: str) -> tuple[_T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise InputError("Expected a sequence", location=location)
    return tuple(value)


def _text(value: str, location: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise InputError("Expected a nonempty string", location=location)
    return value


def _iris(values: tuple[str, ...], location: str) -> tuple[str, ...]:
    return tuple(
        absolute_iri(value, location=location) for value in _sequence(values, location)
    )


@dataclass(frozen=True)
class TermDeclaration:
    """An explicit application IRI and its acceptable RDF declaration kinds."""

    iri: str
    kinds: tuple[str, ...] = ()
    location: str = ""

    def __post_init__(self) -> None:
        absolute_iri(self.iri, location=self.location)
        object.__setattr__(self, "kinds", _iris(self.kinds, self.location))
        _text(self.location, "location", empty=True)


@dataclass(frozen=True)
class ValueDeclaration:
    """One permitted value branch; its RDF types apply simultaneously."""

    kind: ValueKind = "unknown"
    datatype: str | None = None
    language: str | None = None
    types: tuple[str, ...] = ()
    model: str | None = None
    container: str | None = None
    _: KW_ONLY
    samples: tuple[RDFLiteral, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "literal",
            "iri",
            "object",
            "unknown",
        }:
            raise InputError(f"Unknown RDF value kind: {self.kind}")
        object.__setattr__(self, "types", _iris(self.types, "types"))
        if self.datatype is not None:
            absolute_iri(self.datatype, location="datatype")
        if self.language is not None:
            language_tag(self.language, location="language")
        if self.model is not None:
            _text(self.model, "model")
        if self.container is not None:
            _text(self.container, "container")
        if self.kind == "literal" and (self.types or self.model is not None):
            raise InputError("Literal branches cannot declare resource types or models")
        if self.kind in {"iri", "object"} and (
            self.datatype is not None or self.language is not None
        ):
            raise InputError("Resource branches cannot declare literal metadata")
        if self.language is not None and self.datatype not in (
            None,
            str(RDF.langString),
        ):
            raise InputError("A language-tagged literal has datatype rdf:langString")
        samples = _sequence(self.samples, "samples")
        if any(not isinstance(sample, RDFLiteral) for sample in samples):
            raise InputError("Literal samples must be RDFLib Literal values")
        if samples and self.kind != "literal":
            raise InputError("Literal samples belong only to literal branches")
        object.__setattr__(self, "samples", samples)


@dataclass(frozen=True)
class FieldDeclaration:
    """A predicate and alternative values emitted when a field is present."""

    name: str
    predicate: str
    values: tuple[ValueDeclaration, ...]
    key: str | None = None
    optional: bool = False
    location: str = ""
    reverse: bool = False
    _: KW_ONLY
    emission: Emission = "unknown"

    def __post_init__(self) -> None:
        _text(self.name, "name")
        _text(self.location, "location", empty=True)
        _text(self.predicate, "predicate")
        if self.predicate not in {"@id", "@type"}:
            absolute_iri(self.predicate, location=self.location or self.name)
        if self.key is not None:
            _text(self.key, "key")
        if type(self.optional) is not bool or type(self.reverse) is not bool:
            raise InputError("optional and reverse must be booleans")
        if not isinstance(self.emission, str) or self.emission not in {
            "unknown",
            "optional",
            "guaranteed",
        }:
            raise InputError(f"Unknown emission guarantee: {self.emission}")
        values = _sequence(self.values, "values")
        if not values or any(
            not isinstance(value, ValueDeclaration) for value in values
        ):
            raise InputError("A field requires explicit alternative value declarations")
        object.__setattr__(self, "values", values)


@dataclass(frozen=True)
class SubjectIdentity:
    """Application-owned expected RDF subject identity, without OWL inference."""

    expected_iri: str | None = None
    allow_blank: bool = True

    def __post_init__(self) -> None:
        if self.expected_iri is not None:
            absolute_iri(self.expected_iri, location="expected_iri")
        if type(self.allow_blank) is not bool:
            raise InputError("allow_blank must be a boolean")


@dataclass(frozen=True)
class ModelDeclaration:
    """A stable model identity, simultaneous RDF types and serialised fields."""

    identifier: str
    types: tuple[str, ...]
    fields: tuple[FieldDeclaration, ...] = ()
    location: str = ""
    _: KW_ONLY
    subject: SubjectIdentity = SubjectIdentity()

    def __post_init__(self) -> None:
        if not isinstance(self.subject, SubjectIdentity):
            raise InputError("Expected a typed subject identity contract")
        _text(self.identifier, "identifier")
        _text(self.location, "location", empty=True)
        object.__setattr__(self, "types", _iris(self.types, self.identifier))
        fields = _sequence(self.fields, "fields")
        if any(not isinstance(field, FieldDeclaration) for field in fields):
            raise InputError(
                "Expected typed field declarations", location=self.identifier
            )
        by_key: dict[str, FieldDeclaration] = {}
        by_name: dict[str, FieldDeclaration] = {}
        for field in fields:
            for identity, seen in (
                (field.name, by_name),
                (field.key or field.name, by_key),
            ):
                if identity in seen and seen[identity] != field:
                    raise InputError(
                        "Conflicting field occurrence",
                        location=f"{self.identifier}.{identity}",
                    )
                seen[identity] = field
        object.__setattr__(self, "fields", tuple(by_name.values()))
