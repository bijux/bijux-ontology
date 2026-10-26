"""Finite assertion witnesses and equality consequences with exact RDF premises."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef
from rdflib import Literal as RdfLiteral

from bijux_ontology.diagnostics.reports import Report

TermKey = tuple[str | None, ...]
Statement = tuple[TermKey, TermKey, TermKey]
EqualityRule = Literal["same_individual", "functional", "inverse_functional", "key"]


@dataclass(frozen=True)
class PropertyAssertion:
    """An original property assertion with its qualified proof premise."""

    subject: URIRef | BNode
    value: URIRef | BNode | RdfLiteral
    premise: Statement


@dataclass(frozen=True)
class EqualityConsequence:
    """Established individual identity, retaining original asserted premises.

    Resource keys are local to the selected owned source. No new RDF assertion
    is inserted and an equality consequence alone is not a contradiction.
    """

    left: TermKey
    right: TermKey
    rule: EqualityRule
    premises: tuple[Statement, ...]


@dataclass(frozen=True)
class AssertionWitness:
    """One sound finite contradiction under the declared supported scope."""

    rule: str
    premises: tuple[Statement, ...]


@dataclass(frozen=True)
class AssertionWitnesses:
    """Completed finite search evidence, never a general consistency verdict.

    Exact term keys own original lexical/language/datatype text. Named and blank
    resources remain distinct RDF terms even when equality has been established.
    A later exhausted tail retains both known witnesses and incomplete coverage.
    """

    witnesses: tuple[AssertionWitness, ...]
    equalities: tuple[EqualityConsequence, ...]
    report: Report

    @property
    def status(
        self,
    ) -> Literal["contradiction_found", "no_witness_in_selected_scope", "incomplete"]:
        """Separate a positive witness, completed finite absence and stopped work."""
        if self.witnesses:
            return "contradiction_found"
        return "no_witness_in_selected_scope" if self.report.verified else "incomplete"
