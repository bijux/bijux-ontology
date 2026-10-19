"""Representative serializer corpus checking with explicit finite coverage."""

from .checking import CorpusCheck, SerializerCase, check_serializer_corpus
from .types import BranchCoverage, ScenarioCoverage

__all__ = [
    "BranchCoverage",
    "CorpusCheck",
    "ScenarioCoverage",
    "SerializerCase",
    "check_serializer_corpus",
]
