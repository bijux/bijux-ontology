"""Advisory lexical evidence and separately explicit assertion approvals."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.vocabulary.browsing.results import SearchMatch
from bijux_ontology.vocabulary.lexical.types import LexicalIndex


@dataclass(frozen=True)
class LexicalMappingProposal:
    """One directional lexical pair, without a predicate or semantic confidence.

    Rank orders exact spelling before casefold-only matches, then the source
    and target index field priorities. Homonyms retain separate evidence.
    """

    source: SearchMatch
    target: SearchMatch
    comparison: Literal["exact", "casefold"]
    rank: tuple[int, int, int]


@dataclass(frozen=True, init=False, eq=False)
class MappingProposals:
    """Owned advisory results; incomplete output cannot authorize assertions."""

    items: tuple[LexicalMappingProposal, ...]
    report: Report
    limits: Limits

    def __init__(self) -> None:
        """Require actual indexed generation rather than manually assembled truth."""
        raise InputError("Use propose_lexical_mappings to prepare candidates")

    @classmethod
    def _owned(
        cls,
        items: tuple[LexicalMappingProposal, ...],
        report: Report,
        limits: Limits,
    ) -> "MappingProposals":
        """Retain bounded generator products without a mutable source graph."""
        result = object.__new__(cls)
        for name, value in (("items", items), ("report", report), ("limits", limits)):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class MappingApproval:
    """A caller's reviewed proposal, predicate, justification and qualifications.

    An explicit empty qualifications tuple means no additional SSSOM cells.
    Numeric ranks never supply confidence, justification or a mapping predicate.
    """

    proposal: LexicalMappingProposal
    predicate: str
    justification: str
    qualifications: tuple[tuple[str, str], ...]


@dataclass(frozen=True, init=False, eq=False)
class MappingProposalCursor:
    """In-process join position bound to two exact immutable lexical indexes."""

    _source: LexicalIndex
    _target: LexicalIndex
    _language: Literal["same", "any"]
    _position: tuple[int, int, int]

    def __init__(self) -> None:
        """Require a generated continuation rather than manually selected positions."""
        raise InputError(
            "Mapping proposal cursors require propose_lexical_mappings_page"
        )

    @classmethod
    def _at(
        cls,
        source: LexicalIndex,
        target: LexicalIndex,
        language: Literal["same", "any"],
        position: tuple[int, int, int],
    ) -> "MappingProposalCursor":
        """Retain the first uncommitted candidate position and its exact contract."""
        result = object.__new__(cls)
        for name, value in (
            ("_source", source),
            ("_target", target),
            ("_language", language),
            ("_position", position),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True, init=False, eq=False)
class MappingProposalPage:
    """Completed page occurrences may be reviewed without certifying the whole join.

    Order is lexical key, source occurrence, then target occurrence. It is not
    global rank order. Only enumeration_complete certifies the end of the join.
    """

    items: tuple[LexicalMappingProposal, ...]
    report: Report
    limits: Limits
    next_cursor: MappingProposalCursor | None
    enumeration_complete: bool

    def __init__(self) -> None:
        """Require owned completed-page occurrences and qualified continuation."""
        raise InputError("Mapping pages require propose_lexical_mappings_page")

    @classmethod
    def _page(
        cls,
        items: tuple[LexicalMappingProposal, ...],
        report: Report,
        limits: Limits,
        cursor: MappingProposalCursor | None,
        enumeration_complete: bool,
    ) -> "MappingProposalPage":
        """Own the actual retained occurrences and their qualified page status."""
        result = object.__new__(cls)
        for name, value in (
            ("items", items),
            ("report", report),
            ("limits", limits),
            ("next_cursor", cursor),
            ("enumeration_complete", enumeration_complete),
        ):
            object.__setattr__(result, name, value)
        return result
