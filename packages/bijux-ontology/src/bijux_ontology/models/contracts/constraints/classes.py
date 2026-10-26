"""Finite named class and explicit disjointness comparisons for model fields."""

from collections.abc import Sequence

from rdflib import URIRef
from rdflib.namespace import OWL, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.relations.classes import ClassContracts


def class_issues(
    actual: Sequence[str],
    constraints: Sequence[Node],
    classes: ClassContracts,
    location: str,
    role: str,
    budget: Budget,
) -> list[Issue]:
    """Compare named-class obligations without inferring unsupported expressions."""
    issues: list[Issue] = []
    ancestors, anonymous = classes.ancestors(actual)
    named = tuple(str(item) for item in constraints if isinstance(item, URIRef))
    _, required_expression = classes.ancestors(named)
    anonymous |= required_expression or any(
        not isinstance(item, URIRef) for item in constraints
    )
    for conflict in classes.conflicts((*actual, *named)):
        issues.append(
            Issue(
                "models.disjoint_conflict",
                f"{role} types imply explicitly disjoint classes",
                location,
                actual=conflict,
            )
        )
    for constraint in constraints:
        budget.consume("work")
        if isinstance(constraint, URIRef) and (
            str(constraint) in ancestors or constraint in (OWL.Thing, RDFS.Resource)
        ):
            continue
        issues.append(
            Issue(
                "models.relationship_unverified",
                f"{role} relationship is not established by named-class edges",
                location,
                kind="unverified",
                expected=str(constraint)
                if isinstance(constraint, URIRef)
                else "anonymous expression",
                actual=", ".join(sorted(actual)),
            )
        )
    if anonymous:
        issues.append(
            Issue(
                "models.expression_unverified",
                "Relevant class uses an anonymous expression",
                location,
                kind="unverified",
            )
        )
    return issues
