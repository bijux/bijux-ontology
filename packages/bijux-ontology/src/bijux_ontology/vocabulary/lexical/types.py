"""Immutable owned lexical derivatives and explicitly ranked alternatives."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.vocabulary.browsing.results import SearchMatch
from bijux_ontology.vocabulary.sources.view import _LexicalCandidate


@dataclass(frozen=True, init=False, eq=False)
class LexicalIndex:
    """Caller-retained derivative of one owned vocabulary role selection.

    No graph, mutable caller resource or global cache is retained. Case policy,
    selected annotation fields, languages and actual source roles are fixed at
    preparation. A new resource or policy requires a new index.
    """

    fields: tuple[str, ...]
    languages: tuple[str, ...]
    scopes: tuple[str, ...]
    casefold: bool
    limits: Limits
    _keys: tuple[str, ...]
    _entries: Mapping[str, tuple[_LexicalCandidate, ...]]
    _deletions: Mapping[str, tuple[str, ...]] | None
    _priority: Mapping[str, int]

    def __init__(self) -> None:
        """Reject manually assembled indexes without source preparation."""
        raise InputError("Lexical indexes require prepare_lexical_index")

    @classmethod
    def _owned(
        cls,
        fields: tuple[str, ...],
        languages: tuple[str, ...],
        scopes: tuple[str, ...],
        casefold: bool,
        limits: Limits,
        keys: tuple[str, ...],
        entries: Mapping[str, tuple[_LexicalCandidate, ...]],
        deletions: Mapping[str, tuple[str, ...]] | None,
        priority: Mapping[str, int],
    ) -> "LexicalIndex":
        """Adopt only completed bounded immutable preparation products."""
        result = object.__new__(cls)
        for name, value in (
            ("fields", fields),
            ("languages", languages),
            ("scopes", scopes),
            ("casefold", casefold),
            ("limits", limits),
            ("_keys", keys),
            ("_entries", entries),
            ("_deletions", deletions),
            ("_priority", priority),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class LexicalPreparation:
    """A complete reusable derivative, or no index with required nongreen truth."""

    index: LexicalIndex | None
    report: Report


@dataclass(frozen=True)
class LexicalSuggestion:
    """An alternative with original lexical evidence, never an identity rewrite.

    SearchMatch.rank is zero for exact text and one for a prefix/one-edit match.
    Ties use explicit field_priority, normalized text, term IRI, language, original
    value and source. Equal-ranked homonyms remain independent alternatives.
    """

    match: SearchMatch
    kind: Literal["exact", "prefix", "one-edit"]
    edit_distance: int | None
    field_priority: int


@dataclass(frozen=True)
class LexicalResults:
    """Top alternatives; truncation differs from interrupted query work.

    A complete report means the requested top results were computed. Truncated
    says additional matches existed. Incomplete results are only a proved prefix
    of observed alternatives and cannot certify a global top order or absence.
    """

    query: str
    items: tuple[LexicalSuggestion, ...]
    truncated: bool
    report: Report
