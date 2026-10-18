"""Explicit authoritative owner declarations and original selected evidence."""

from dataclasses import dataclass

from bijux_ontology.errors import InputError
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.roles.types import TermRole
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.facts.types import (
    FactPredicate,
    SelectedFact,
    SelectedFactDifferences,
)
from bijux_ontology.vocabulary.inspection import VocabularyView


@dataclass(frozen=True)
class OwnerClaim:
    """An exclusive caller-declared owner, backed by an actual selected view.

    Select exactly one term or namespace IRI. Exact terms override namespace
    claims; the longest matching namespace wins. A role facet refines a claim
    only for that actual subject occurrence. Source presence does not infer an
    owner, and namespace overlap alone does not establish a conflict.
    """

    owner: str
    source: VocabularyView
    term: str | None = None
    namespace: str | None = None
    role: TermRole | None = None

    def __post_init__(self) -> None:
        """Reject ambiguous declarations before policy execution."""
        if type(self.owner) is not str or not self.owner.strip():
            raise InputError("Owner claims require a nonempty publisher identity")
        if not isinstance(self.source, VocabularyView):
            raise InputError("Owner claims require an actual selected vocabulary view")
        if (self.term is None) == (self.namespace is None):
            raise InputError("Select exactly one owned term or namespace")
        for selector in (self.term, self.namespace):
            if selector is not None:
                absolute_iri(selector)
        FactPredicate("urn:bijux:ownership:role", subject_role=self.role)


@dataclass(frozen=True)
class OwnershipEvidence:
    """One occurrence facet and its original scoped facts plus content comparison.

    The comparison projects ground assertion content only. Original source and
    graph identity remain in current and authoritative; the projection does not
    assert source, graph, document or whole-ontology equivalence.
    """

    term: str
    role: TermRole
    claims: tuple[OwnerClaim, ...]
    current: tuple[SelectedFact, ...]
    authoritative: tuple[SelectedFact, ...]
    comparison: SelectedFactDifferences | None = None


@dataclass(frozen=True)
class OwnershipCheck:
    """Selected policy decisions, raw descriptive comparisons and required truth."""

    evidence: tuple[OwnershipEvidence, ...]
    report: Report
