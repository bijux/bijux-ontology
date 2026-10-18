"""Original anonymous neighborhoods and syntactic named attachments."""

from dataclasses import dataclass, field

from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.facts.types import FactGraph, SelectedFact


@dataclass(frozen=True)
class NamedAttachment:
    """A named subject's actual edge into an anonymous neighborhood.

    An attachment establishes syntactic attribution, never unique axiom identity
    or the consequences of an arbitrary OWL expression.
    """

    owner: str
    predicate: str
    source_role: str
    graph: FactGraph


@dataclass(frozen=True)
class AnonymousChange:
    """One changed joint neighborhood grouped by every named attachment."""

    attachments: tuple[NamedAttachment, ...]
    before: tuple[SelectedFact, ...]
    after: tuple[SelectedFact, ...]
    references: tuple[str, ...]
    qualified: bool


@dataclass(frozen=True)
class AnonymousDifferences:
    """Bounded attachment attribution and untouched side-local RDF evidence."""

    changes: tuple[AnonymousChange, ...]
    report: Report
    before: tuple[SelectedFact, ...] = field(default=(), kw_only=True)
    after: tuple[SelectedFact, ...] = field(default=(), kw_only=True)
