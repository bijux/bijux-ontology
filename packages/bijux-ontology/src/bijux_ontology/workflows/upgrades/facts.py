"""Join selected assertion positions to the logical facets actually used."""

from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef
from rdflib.namespace import RDF

from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.roles.classification import classify_statement
from bijux_ontology.rdf.roles.types import TermRole, TermRoleOccurrence
from bijux_ontology.reports import Issue
from bijux_ontology.vocabulary.comparison import Change
from bijux_ontology.vocabulary.facts.types import (
    FactSelection,
    SelectedFactDifference,
    SelectedFactDifferences,
)
from bijux_ontology.vocabulary.sources.types import source_role

from .uses import Use, UseIndex


@dataclass(frozen=True)
class FactAffectedUse:
    """A precise changed assertion position and its definite or uncertain use."""

    difference: SelectedFactDifference
    occurrence: TermRoleOccurrence
    use: Use
    relation: Literal["direct", "uncertain"]


def role_matches(use: Use, role: TermRole) -> bool | None:
    """A generic term reference is broad; chosen logical facets remain separate."""
    if use.role == "term":
        return True
    if role == "unknown":
        return None
    if use.role == "predicate":
        return role in (
            "property",
            "object_property",
            "datatype_property",
            "annotation_property",
        )
    return use.role == role


def change_roles(
    change: Change,
    iri: str,
    selected: SelectedFactDifferences | None,
    selection: FactSelection | None,
    budget: Budget,
) -> frozenset[TermRole]:
    """Qualify categorized evidence without assigning one role to the whole fact."""
    if not change.iri:
        return frozenset()
    values = (*change.before, *change.after)
    predicate = change.predicate
    if predicate is None and change.kind in ("term-added", "term-removed", "kinds"):
        predicate = str(RDF.type)
        if change.kind == "kinds":
            values = tuple(set(change.before) ^ set(change.after))
    if predicate is None:
        return frozenset()
    roles: set[TermRole] = set()
    if selected is not None:
        for difference in selected.differences:
            budget.consume("work")
            fact = difference.fact
            scopes = (
                change.before_scopes
                if difference.status == "removed"
                else change.after_scopes
            )
            if (
                fact.source_scope not in scopes
                or str(fact.statement[0]) != change.iri
                or str(fact.statement[1]) != predicate
                or fact.statement[2] not in values
            ):
                continue
            for occurrence in fact.occurrences:
                budget.consume("work")
                if (
                    isinstance(occurrence.term, URIRef)
                    and str(occurrence.term) == iri
                    and (iri != change.iri or occurrence.position == "subject")
                ):
                    budget.consume("states")
                    roles.add(occurrence.role)
        if selection is not None:
            budget.consume(
                "work",
                len(selection.entities)
                + len(selection.predicates)
                + len(change.before_scopes)
                + len(change.after_scopes),
            )
        covered = (
            selection is not None
            and selection.graphs is None
            and (
                not selection.entities
                or "subject" in selection.positions
                and change.iri in selection.entities
            )
            and all(
                source_role(scope) in selection.scopes
                for scope in (*change.before_scopes, *change.after_scopes)
            )
            and any(rule.iri == predicate for rule in selection.predicates)
        )
        if covered and selected.report.complete:
            return frozenset(roles)
    for value in values:
        budget.consume("work")
        for occurrence in classify_statement(
            (URIRef(change.iri), URIRef(predicate), value),
            budget=budget,
        ):
            # Categorized owner identity denotes the subject; selected fact
            # results retain every distinct term position.
            if (
                isinstance(occurrence.term, URIRef)
                and str(occurrence.term) == iri
                and (iri != change.iri or occurrence.position == "subject")
            ):
                budget.consume("states")
                roles.add(occurrence.role)
    return frozenset(roles)


def selected_fact_uses(
    differences: SelectedFactDifferences,
    uses: UseIndex,
    budget: Budget,
    issues: IssueCollector,
    result: list[FactAffectedUse],
) -> None:
    """Retain exact per-position impact and mark unresolved role joins explicitly."""
    by_iri: dict[str, list[Use]] = defaultdict(list)
    for use in uses.entries:
        budget.consume("work")
        budget.consume("states")
        by_iri[use.iri].append(use)
    for difference in differences.differences:
        for occurrence in difference.fact.occurrences:
            budget.consume("work")
            if not isinstance(occurrence.term, URIRef):
                continue
            for use in by_iri.get(str(occurrence.term), ()):
                budget.consume("work")
                matched = role_matches(use, occurrence.role)
                if matched is False:
                    continue
                if matched is None:
                    issues.append(
                        Issue(
                            "upgrade.fact_impact_unverified",
                            "Selected fact occurrence has no established "
                            "role for this use",
                            use.location,
                            kind="unverified",
                        )
                    )
                budget.consume("states")
                result.append(
                    FactAffectedUse(
                        difference,
                        occurrence,
                        use,
                        "direct" if matched else "uncertain",
                    )
                )
