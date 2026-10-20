"""Owned lexical autocomplete and explicitly bounded advisory alternatives."""

from .annotation import TextAnnotation, TextMention, annotate_text
from .automaton import MentionIndex, MentionPreparation, prepare_mention_index
from .preparation import prepare_lexical_index, prepare_typo_index
from .queries import autocomplete, suggest
from .types import LexicalIndex, LexicalPreparation, LexicalResults, LexicalSuggestion

__all__ = [
    "LexicalIndex",
    "LexicalPreparation",
    "LexicalResults",
    "LexicalSuggestion",
    "MentionIndex",
    "MentionPreparation",
    "TextAnnotation",
    "TextMention",
    "annotate_text",
    "autocomplete",
    "prepare_lexical_index",
    "prepare_mention_index",
    "prepare_typo_index",
    "suggest",
]
