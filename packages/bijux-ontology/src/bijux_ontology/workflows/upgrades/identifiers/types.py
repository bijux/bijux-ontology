"""Exact used occurrences, supplied release policies and advisory update evidence."""

from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.accessions.types import (
    AccessionCheck,
    AccessionObservation,
    AccessionRule,
    AccessionValue,
    _text,
)
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

Transition = Literal["introduced", "persistent", "resolved", "unchanged", "uncertain"]
Change = Literal[
    "expansion_changed",
    "canonical_spelling_changed",
    "recognition_changed",
    "recognition_became_ambiguous",
]
Verdict = Literal["demonstrated-break", "unverified", "informational", "agreement"]


@dataclass(frozen=True)
class IdentifierRelease:
    """An explicit identifier policy, without version ordering or RDF existence."""

    label: str
    registry: IdentifierRegistry
    rules: tuple[AccessionRule, ...]

    def __post_init__(self) -> None:
        """Own descriptive metadata and require immutable maintained policy inputs."""
        operation = Budget()
        label = _text(self.label, operation)
        if not label or not isinstance(self.registry, IdentifierRegistry):
            raise InputError("Identifier releases require a label and registry")
        if type(self.rules) is not tuple:
            raise InputError("Identifier releases require an immutable rule tuple")
        operation.consume("states", len(self.rules))
        for rule in self.rules:
            operation.consume("work")
            if not isinstance(rule, AccessionRule):
                raise InputError("Identifier releases require typed rules")
        object.__setattr__(self, "label", label)


@dataclass(frozen=True)
class IdentifierEquivalence:
    """One caller declaration of exact IRI correspondence, without inference."""

    before: URIRef
    after: URIRef

    def __post_init__(self) -> None:
        """Reject erased RDF kinds or namespace-wide wildcard interpretations."""
        operation = Budget()
        for term in (self.before, self.after):
            if not isinstance(term, URIRef):
                raise InputError("Identifier equivalence requires exact named IRIs")
            bound_node(term, operation)


@dataclass(frozen=True)
class IdentifierImpact:
    """Independent mapping, spelling and validation evidence for one occurrence."""

    original: AccessionValue
    before: AccessionObservation | None
    after: AccessionObservation | None
    before_expansion: str | None
    after_expansion: str | None
    changes: tuple[Change, ...]
    validation: Transition


@dataclass(frozen=True)
class IdentifierUpdate:
    """An explicitly authorized advisory candidate and its actual after check."""

    original: AccessionValue
    equivalence: IdentifierEquivalence
    candidate: AccessionValue | None
    after: AccessionCheck | None
    admissible: bool | None


@dataclass(frozen=True)
class IdentifierReleaseImpact:
    """Finite behavior differences and raw policies, never migration acceptance."""

    before: AccessionCheck | None
    after: AccessionCheck | None
    uses: tuple[IdentifierImpact, ...]
    updates: tuple[IdentifierUpdate, ...]
    verdict: Verdict
    report: Report
