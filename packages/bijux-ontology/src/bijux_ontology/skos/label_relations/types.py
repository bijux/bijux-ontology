"""Explicit SKOS-XL relation policies and original assertion witnesses."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from rdflib import BNode
from rdflib import Literal as RdfLiteral

from bijux_ontology.absence import Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.vocabulary.browsing.results import Edge

from ..xl import XLLabel


def _text(value: str) -> None:
    """Bound constructor-owned text before retaining a policy inventory."""
    if len(value) > Limits().max_bytes:
        raise LimitExceeded("XL policy text exceeds max_bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("XL policy requires valid Unicode text") from error
    if size > Limits().max_bytes:
        raise LimitExceeded("XL policy text exceeds max_bytes")


def _iri(value: object) -> str:
    if not isinstance(value, str) or isinstance(value, BNode | RdfLiteral):
        raise InputError("XL relation predicates require named IRI strings")
    _text(value)
    absolute_iri(value)
    return value


@dataclass(frozen=True)
class XLRelationPolicy:
    """Select exact predicates and finite constraints on their label endpoints.

    Subrelations are caller declarations, not a request to infer a property
    hierarchy. Direction applies to supplied assertions; either permits an
    explicitly reversed endpoint check without asserting a reverse triple.
    Language None is unchecked, ABSENT requires no tag, and text selects a tag.
    Form comparisons use exact RDF literal terms, not label resource identity.
    """

    predicate: str
    subrelations: tuple[str, ...] = ()
    forbidden: bool = False
    direction: Literal["asserted", "either"] = "asserted"
    source_language: str | Absent | None = None
    target_language: str | Absent | None = None
    language_relation: Literal["any", "same", "different"] = "any"
    form_relation: Literal["any", "same", "different"] = "any"

    def __post_init__(self) -> None:
        _iri(self.predicate)
        if not isinstance(self.subrelations, Sequence) or isinstance(
            cast(object, self.subrelations), (str, bytes)
        ):
            raise InputError("XL subrelations require a finite IRI sequence")
        count = len(self.subrelations)
        if count > Limits().max_states:
            raise LimitExceeded("XL subrelations exceed max_states")
        owned = tuple(_iri(self.subrelations[index]) for index in range(count))
        if len({self.predicate, *owned}) != len(owned) + 1:
            raise InputError("XL predicate inventories require unique IRIs")
        if type(self.forbidden) is not bool:
            raise InputError("XL forbidden policy requires a boolean")
        if self.direction not in ("asserted", "either"):
            raise InputError("Select an asserted or either XL relation direction")
        for comparison in (self.language_relation, self.form_relation):
            if comparison not in ("any", "same", "different"):
                raise InputError("Select an any, same or different XL comparison")
        for language in (self.source_language, self.target_language):
            if language is not None and not isinstance(language, Absent):
                if not isinstance(language, str):
                    raise InputError("XL language requires a tag, ABSENT or None")
                _text(language)
                language_tag(language)
        object.__setattr__(self, "subrelations", owned)


@dataclass(frozen=True)
class XLRelation:
    """Keep original endpoints and their proved orientation without merging labels."""

    assertion: Edge
    source: XLLabel | None
    target: XLLabel | None
    matches: bool | None
    orientation: Literal["forward", "reversed"] | None = None
