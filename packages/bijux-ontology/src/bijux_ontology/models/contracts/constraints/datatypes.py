"""Finite declared datatype relationships under the selected literal regime."""

from collections.abc import Sequence

from rdflib import URIRef
from rdflib.namespace import RDF, RDFS
from rdflib.term import Node

from bijux_ontology.declarations.types import (
    ValueDeclaration,
)
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.owl.relations.classes import ClassContracts
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import (
    Regime,
    datatype_relation,
)


def datatype_issues(
    value: ValueDeclaration,
    ranges: Sequence[Node],
    location: str,
    classes: ClassContracts,
    regime: Regime,
    budget: Budget,
    *,
    encoded: bool = False,
    datatype_registry: DatatypeRegistry | None = None,
) -> list[Issue]:
    """Compare declared literal ranges under the original datatype regime."""
    datatype = (
        str(RDF.langString) if isinstance(value.language, str) else value.datatype
    )
    issues: list[Issue] = []
    if (
        not encoded
        and datatype
        and datatype_relation(
            datatype,
            datatype,
            regime=regime,
            datatype_registry=datatype_registry,
            budget=budget,
        )
        == "unknown"
    ):
        issues.append(
            Issue(
                "models.datatype_unverified",
                "Declared datatype semantics are unsupported",
                location,
                kind="unverified",
                actual=datatype,
            )
        )
    for required in ranges:
        budget.consume("work")
        if required in (RDFS.Literal, RDFS.Resource):
            continue
        expected = (
            str(required) if isinstance(required, URIRef) else "anonymous expression"
        )
        expression = not isinstance(required, URIRef) or bool(
            classes.ancestors((expected,))[1]
        )
        relation = (
            datatype_relation(
                datatype,
                expected,
                regime=regime,
                datatype_registry=datatype_registry,
                budget=budget,
            )
            if datatype and not expression
            else "unknown"
        )
        if relation in ("equal", "subset"):
            continue
        conflict = relation == "disjoint"
        issue = Issue(
            "models.datatype_conflict" if conflict else "models.datatype_unverified",
            "Literal serialisation conflicts with the declared datatype range"
            if conflict
            else "Datatype compatibility is outside the supported comparisons",
            location,
            kind="finding" if conflict else "unverified",
            expected=expected,
            actual=datatype,
        )
        issues.append(issue)
    return issues
