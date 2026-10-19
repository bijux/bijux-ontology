"""Explicit annotation shape and per-language publication requirements."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeVar, cast

from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral

from bijux_ontology.absence import Absent
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.vocabulary.browsing.results import Edge

from ..languages.matching import matches_language

T = TypeVar("T")
LanguageMode = Literal["exact", "basic", "extended"]
ValueKind = Literal["literal", "iri", "blank"]


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), (str, bytes)
    ):
        raise InputError("Annotation inventories require finite sequences")
    count = len(values)
    budget.consume("states", count)
    return tuple(values[index] for index in range(count))


def _text(value: str, budget: Budget) -> None:
    if not isinstance(value, str):
        raise InputError("Annotation contract requires text")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Annotation text exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Annotation text requires valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Annotation text exceeds max_bytes")
    budget.consume("bytes", size)


def _iri(value: str, budget: Budget) -> None:
    if isinstance(value, BNode | RdfLiteral):
        raise InputError("Annotation identifiers require named IRI strings")
    _text(value, budget)
    absolute_iri(value)


def _bounds(minimum: int, maximum: int | None) -> None:
    if (
        type(minimum) is not int
        or minimum < 0
        or (maximum is not None and (type(maximum) is not int or maximum < minimum))
    ):
        raise InputError("Annotation counts require nonnegative ordered bounds")


def _language(value: str | Absent, mode: LanguageMode, budget: Budget) -> None:
    if isinstance(value, Absent):
        return
    if mode == "exact":
        _text(value, budget)
        language_tag(value)
    else:
        matches_language(None, value, extended=mode == "extended", budget=budget)


@dataclass(frozen=True)
class LanguageRequirement:
    """Count one language/range, or explicit untagged literals via ABSENT.

    Fallback is a finite list of explicit alternatives using the same matching
    mode. It is considered only when the complete primary inventory is empty;
    it never fills a nonempty primary inventory's minimum deficit. No lookup or
    implicit parent-region equivalence is performed.
    """

    language: str | Absent
    minimum: int = 1
    maximum: int | None = None
    mode: LanguageMode = "exact"
    fallback: tuple[str | Absent, ...] = ()

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        if self.mode not in ("exact", "basic", "extended"):
            raise InputError("Select exact, basic or extended language coverage")
        operation = Budget()
        _language(self.language, self.mode, operation)
        owned = _capture(self.fallback, operation)
        for value in owned:
            _language(value, self.mode, operation)
        if len({self.language, *owned}) != len(owned) + 1:
            raise InputError("Language fallback alternatives must be distinct")
        object.__setattr__(self, "fallback", owned)


@dataclass(frozen=True)
class AnnotationRequirement:
    """Apply explicit value shape/count rules to one annotation predicate."""

    predicate: str
    value_kinds: tuple[ValueKind, ...] = ("literal",)
    datatypes: tuple[str, ...] = ()
    minimum: int = 1
    maximum: int | None = None
    languages: tuple[LanguageRequirement, ...] = ()

    def __post_init__(self) -> None:
        _bounds(self.minimum, self.maximum)
        operation = Budget()
        _iri(self.predicate, operation)
        kinds = _capture(self.value_kinds, operation)
        if not kinds or any(
            value not in ("literal", "iri", "blank") for value in kinds
        ):
            raise InputError("Select supported annotation value kinds")
        if len(set(kinds)) != len(kinds):
            raise InputError("Annotation value kinds must be unique")
        datatypes = _capture(self.datatypes, operation)
        for datatype in datatypes:
            _iri(datatype, operation)
        if len(set(datatypes)) != len(datatypes):
            raise InputError("Annotation datatypes must be unique")
        languages = _capture(self.languages, operation)
        if any(not isinstance(value, LanguageRequirement) for value in languages):
            raise InputError("Expected typed LanguageRequirement values")
        if languages and "literal" not in kinds:
            raise InputError("Language coverage requires permitted literal values")
        if len(set(languages)) != len(languages):
            raise InputError("Language requirements must be unique")
        object.__setattr__(self, "value_kinds", kinds)
        object.__setattr__(self, "datatypes", datatypes)
        object.__setattr__(self, "languages", languages)


@dataclass(frozen=True)
class LanguageCoverage:
    """Retain the chosen explicit primary/fallback tier and distinct values."""

    requirement: LanguageRequirement
    selected: str | Absent | None
    used_fallback: bool
    values: tuple[Edge, ...]
    complete: bool


@dataclass(frozen=True)
class AnnotationCoverage:
    """One selected entity/predicate retains every supplied annotation assertion."""

    entity: URIRef
    predicate: URIRef
    values: tuple[Edge, ...]
    languages: tuple[LanguageCoverage, ...]
    complete: bool
