"""Advisory lexical evidence and separately explicit assertion approvals."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.results import SearchMatch


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
