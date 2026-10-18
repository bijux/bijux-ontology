"""Snapshot-bound continuations and explicit vocabulary page completion."""

from dataclasses import dataclass, field
from typing import Generic, TypeVar

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.view import _VocabularyIndex

T = TypeVar("T")


@dataclass(frozen=True, init=False, eq=False)
class VocabularyCursor:
    """Opaque in-process continuation; retain its immutable snapshot, never a graph.

    Cursors are issued by page operations. They are neither serializable tokens
    nor portable blank-node identifiers, and cannot follow a replaced snapshot.
    """

    _index: _VocabularyIndex = field(repr=False)
    _contract: tuple[object, ...] = field(repr=False)
    _position: int
    _phase: int

    def __init__(self) -> None:
        """Reject manual construction of an unbound continuation."""
        raise InputError("Vocabulary cursors must be issued by a page operation")

    @classmethod
    def _at(
        cls,
        index: _VocabularyIndex,
        contract: tuple[object, ...],
        position: int,
        phase: int,
    ) -> "VocabularyCursor":
        """Own the exact continuation after the last fully processed candidate."""
        result = object.__new__(cls)
        object.__setattr__(result, "_index", index)
        object.__setattr__(result, "_contract", contract)
        object.__setattr__(result, "_position", position)
        object.__setattr__(result, "_phase", phase)
        return result


@dataclass(frozen=True)
class VocabularyPage(Generic[T]):
    """Completed page work does not imply completed enumeration.

    Only enumeration_complete certifies that the requested stream was consumed.
    An interrupted page retains proved items and its resumable next_cursor.
    """

    items: tuple[T, ...]
    next_cursor: VocabularyCursor | None
    enumeration_complete: bool
    report: Report
