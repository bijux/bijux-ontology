"""Declared replacement candidates remain suggestions with explicit limitations."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge, Selection

from ..errors import LimitExceeded
from ..reports import Issue, Report
from .builtins import Regime, builtin_kinds, kind_matches, validate_regime


@dataclass(frozen=True)
class ReplacementCandidate:
    """One original annotation and its finite declared-role compatibility."""

    assertion: Edge
    status: Literal["declared", "unresolved", "kind-mismatch", "unverified"]
    kinds: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()


def _expected(kinds: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Retain supported simultaneous roles without interpreting arbitrary types."""
    values = set(kinds)
    if str(OWL.DeprecatedClass) in values:
        values.add(str(OWL.Class))
    if str(OWL.DeprecatedProperty) in values:
        values.add(str(RDF.Property))
    roles: list[tuple[str, ...]] = []
    if values & {str(OWL.Class), str(RDFS.Class)}:
        roles.append((str(OWL.Class), str(RDFS.Class)))
    for kind in (
        OWL.ObjectProperty,
        OWL.DatatypeProperty,
        OWL.AnnotationProperty,
        RDFS.Datatype,
    ):
        if str(kind) in values:
            roles.append((str(kind),))
    if str(RDF.Property) in values and not any(
        str(kind) in values
        for kind in (
            OWL.ObjectProperty,
            OWL.DatatypeProperty,
            OWL.AnnotationProperty,
        )
    ):
        roles.append((str(RDF.Property),))
    return tuple(roles)


def replacement_candidates(
    view: VocabularyView,
    iri: str,
    *,
    predicates: Sequence[str] = (),
    regime: Regime = "rdf",
    budget: Budget | None = None,
) -> Selection[ReplacementCandidate]:
    """Inspect configured hints without rewriting or claiming logical equivalence.

    No hints means an absent suggestion, not a missing required field. Multiple
    hints retain separate assertions; a caller must make any replacement choice.
    A role mismatch is a supplied declaration mismatch, not an OWL contradiction.
    """
    validate_regime(regime)
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    original = view.lookup(iri)
    if not predicates:
        return Selection(
            (),
            Report(
                "terms.replacements",
                0,
                allow_empty=True,
                regime=regime,
                families=("deprecation.replacements",),
            ),
        )
    selected = view.relationships(iri, predicates=predicates, budget=operation)
    list.extend(issues, selected.report.issues)
    expected = _expected(original.kinds)
    candidates: list[ReplacementCandidate] = []
    complete = selected.report.complete
    try:
        for edge in selected.items:
            operation.consume("work")
            operation.consume("states")
            if not isinstance(edge.object, URIRef):
                candidates.append(ReplacementCandidate(edge, "unverified"))
                issues.append(
                    Issue(
                        "ontology.replacement_shape_unverified",
                        "Configured replacement hint is not a named IRI",
                        iri,
                        kind="unverified",
                        actual=edge.object.n3(),
                    )
                )
                continue
            target = view.lookup(str(edge.object))
            kinds = frozenset(target.kinds) | builtin_kinds(str(edge.object), regime)
            if not kinds:
                candidates.append(
                    ReplacementCandidate(edge, "unresolved", scopes=target.scopes)
                )
                issues.append(
                    Issue(
                        "ontology.replacement_unresolved",
                        "Candidate declaration is unavailable in the selected scope",
                        iri,
                        kind="unverified",
                        actual=str(edge.object),
                    )
                )
            elif not expected:
                candidates.append(
                    ReplacementCandidate(
                        edge, "unverified", tuple(sorted(kinds)), target.scopes
                    )
                )
                issues.append(
                    Issue(
                        "ontology.replacement_kind_unverified",
                        "Original term has no supported declared role to compare",
                        iri,
                        kind="unverified",
                        actual=str(edge.object),
                    )
                )
            elif not all(kind_matches(kinds, role, regime) for role in expected):
                candidates.append(
                    ReplacementCandidate(
                        edge, "kind-mismatch", tuple(sorted(kinds)), target.scopes
                    )
                )
                issues.append(
                    Issue(
                        "ontology.replacement_kind_mismatch",
                        "Candidate lacks a supported original declaration role",
                        iri,
                        severity="warning",
                        expected=repr(expected),
                        actual=str(edge.object),
                    )
                )
            else:
                candidates.append(
                    ReplacementCandidate(
                        edge, "declared", tuple(sorted(kinds)), target.scopes
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error), location=iri)
        complete = False
    return Selection(
        tuple(candidates),
        Report(
            "terms.replacements",
            len(candidates),
            tuple(issues),
            allow_empty=True,
            requested=len(candidates) if complete else len(candidates) + 1,
            complete=complete,
            regime=regime,
            families=("deprecation.replacements",),
        ),
    )
