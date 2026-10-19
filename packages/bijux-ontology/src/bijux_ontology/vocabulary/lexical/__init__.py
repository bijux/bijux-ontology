"""Owned lexical autocomplete and explicitly bounded advisory alternatives."""

from .annotation import TextAnnotation, TextMention, annotate_text
from .preparation import prepare_lexical_index
from .queries import autocomplete, suggest
from .types import LexicalIndex, LexicalPreparation, LexicalResults, LexicalSuggestion

__all__ = [
    "LexicalIndex",
    "LexicalPreparation",
    "LexicalResults",
    "LexicalSuggestion",
    "TextAnnotation",
    "TextMention",
    "annotate_text",
    "autocomplete",
    "prepare_lexical_index",
    "suggest",
]
