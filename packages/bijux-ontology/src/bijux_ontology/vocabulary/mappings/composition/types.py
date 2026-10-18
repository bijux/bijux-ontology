"""Relation-specific candidates preserve original mapping occurrences and bases."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.mappings.types import MappingRow, MappingSet


@dataclass(frozen=True)
class MappingCompositionRule:
    """One caller-selected relation chain with an explicit interpretation basis.

    Only exactMatch followed by exactMatch producing exactMatch has the supported
    SKOS basis. Other chains require an application basis and its named policy
    identifier; approving such a policy never turns it into a SKOS entailment.
    """

    left: str
    right: str
    result: str
    basis: Literal["skos", "application"]
    identifier: str


@dataclass(frozen=True)
class MappingOccurrence:
    """Actual original row position and its complete immutable source metadata."""

    mapping_set: MappingSet
    index: int

    @property
    def row(self) -> MappingRow:
        """Return the original row, including lexical confidence and qualifiers."""
        return self.mapping_set.rows[self.index]


@dataclass(frozen=True)
class MappingCompositionPath:
    """One actual selected join, including uninterpreted and unapproved chains."""

    intermediate: str
    left: MappingOccurrence
    right: MappingOccurrence
    status: Literal["derived", "no_rule", "unverified"]


@dataclass(frozen=True)
class ComposedMapping:
    """An endpoint candidate attributed to one original path and explicit rule.

    Multiple paths and targets remain multiple candidates. Confidence stays on
    the premises and is never multiplied or interpreted as a logical proof.
    """

    subject: str
    predicate: str
    object: str
    path: MappingCompositionPath
    rule: MappingCompositionRule


@dataclass(frozen=True)
class MappingComposition:
    """Bounded selected joins, candidates and retained raw producer reports.

    Inputs remain available in full after qualification, including unmatched
    rows and all source metadata. Results are mapping candidates, not OWL
    identity assertions or an exported SSSOM set with fabricated provenance.
    """

    left: MappingSet | None
    right: MappingSet | None
    intermediates: tuple[str, ...]
    rules: tuple[MappingCompositionRule, ...]
    paths: tuple[MappingCompositionPath, ...]
    results: tuple[ComposedMapping, ...]
    report: Report
