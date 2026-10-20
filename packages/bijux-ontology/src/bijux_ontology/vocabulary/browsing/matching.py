"""One qualified exact/substring matching and rank contract for vocabulary search."""

from collections.abc import Sequence
from dataclasses import dataclass

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget
from bijux_ontology.vocabulary.browsing.results import SearchMatch
from bijux_ontology.vocabulary.sources.view import _LexicalCandidate

from .admission import _comparison_text, _filter_set, _strings, _text


@dataclass(frozen=True)
class _Search:
    """Owned field/language selection and Unicode comparison policy."""

    query: str
    fields: tuple[str, ...]
    languages: tuple[str, ...]
    exact: bool
    casefold: bool
    _fields: frozenset[str]
    _languages: frozenset[str]

    @classmethod
    def prepare(
        cls,
        query: str,
        fields: Sequence[str],
        languages: Sequence[str],
        exact: bool,
        casefold: bool,
        budget: Budget,
    ) -> "_Search":
        """Validate and fund options once for an entire eager read or page."""
        if not isinstance(query, str) or not query:
            raise InputError("Search requires nonempty text")
        if type(exact) is not bool or type(casefold) is not bool:
            raise InputError("Search exact/casefold options must be booleans")
        _text(query, budget)
        wanted = _strings(fields, budget, iri_field=True)
        tags = _strings(languages, budget, languages=True)
        compared = _comparison_text(query, budget, fold=True) if casefold else query
        return cls(
            compared,
            wanted,
            tags,
            exact,
            casefold,
            frozenset(),
            frozenset(),
        )

    def admit(self, budget: Budget) -> "_Search":
        """Prepare matching membership inside the operation's result boundary."""
        return type(self)(
            self.query,
            self.fields,
            self.languages,
            self.exact,
            self.casefold,
            _filter_set(self.fields, budget),
            _filter_set(self.languages, budget),
        )

    def match(
        self,
        candidate: _LexicalCandidate,
        phase: int,
        budget: Budget,
    ) -> SearchMatch | None:
        """Retain original RDF evidence only at its explicit lexical rank."""
        field = _comparison_text(candidate.field, budget)
        if field not in self._fields:
            return None
        if self._languages and field != "iri":
            language = candidate.language or ""
            budget.consume("work", len(language))
            budget.consume("bytes", 64 + 4 * len(language))
            if language.lower() not in self._languages:
                return None
        value = _comparison_text(candidate.value, budget)
        compared = (
            _comparison_text(value, budget, fold=True) if self.casefold else value
        )
        budget.consume("work", len(compared) + len(self.query))
        equal = compared == self.query
        if not equal and (self.exact or self.query not in compared):
            return None
        rank = 0 if field == "iri" and equal else 1 if equal else 2
        if rank != phase:
            return None
        budget.consume("bytes", 128)
        return SearchMatch(
            candidate.term, field, value, candidate.language, candidate.scope, rank
        )
