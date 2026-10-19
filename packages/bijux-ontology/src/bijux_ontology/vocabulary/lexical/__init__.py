"""Owned lexical autocomplete and explicitly bounded advisory alternatives."""

from .preparation import prepare_lexical_index
from .queries import autocomplete, suggest
from .types import LexicalIndex, LexicalPreparation, LexicalResults, LexicalSuggestion

__all__ = [
    "LexicalIndex",
    "LexicalPreparation",
    "LexicalResults",
    "LexicalSuggestion",
    "autocomplete",
    "prepare_lexical_index",
    "suggest",
]
