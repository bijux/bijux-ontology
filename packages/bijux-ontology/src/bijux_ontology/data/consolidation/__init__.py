"""Owned corpus entity descriptions with explicit agreement and postconditions."""

from .checking import consolidate_entities
from .indexing import prepare_corpus
from .types import (
    Consolidation,
    ConsolidationCheck,
    CorpusContribution,
    CorpusDocument,
    CorpusIndex,
    CorpusOrigin,
    CorpusSnapshot,
    EntityDescription,
    PredicateAgreement,
)

__all__ = [
    "Consolidation",
    "ConsolidationCheck",
    "CorpusContribution",
    "CorpusDocument",
    "CorpusIndex",
    "CorpusOrigin",
    "CorpusSnapshot",
    "EntityDescription",
    "PredicateAgreement",
    "consolidate_entities",
    "prepare_corpus",
]
