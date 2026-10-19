"""Check only established vocabulary positions against supplied finite inventories."""

from rdflib import Graph
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .types import QueryUsage


def check_query_usage(
    usage: QueryUsage,
    vocabulary: Vocabulary | Graph,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Retain extraction unknowns beside real role and deprecation checks."""
    if type(usage) is not QueryUsage:
        raise InputError("Query checks require actual read_query_usage evidence")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Query checks require Limits and Budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Query check limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    children = [usage.report]
    checked = 0
    complete = False
    try:
        retain_report(usage.report, operation, issues)
        inventory = (
            vocabulary
            if isinstance(vocabulary, Vocabulary)
            else load_vocabulary(vocabulary, budget=operation)
        )
        kinds = {
            "class": (str(OWL.Class), str(RDFS.Class)),
            "predicate": (str(RDF.Property),),
            "datatype": (str(RDFS.Datatype),),
        }
        terms = []
        operation.consume("states", len(usage.references) * 3 + 1)
        operation.consume("bytes", 128 * len(usage.references) + 128)
        for reference in usage.references:
            operation.consume("work")
            if reference.role in kinds:
                bound_text(reference.iri, operation)
                bound_text(reference.location, operation)
                terms.append(
                    TermDeclaration(
                        reference.iri, kinds[reference.role], reference.location
                    )
                )
        result = check_vocabulary(inventory, terms, budget=operation, allow_empty=True)
        children.append(result)
        retain_report(result, operation, issues)
        checked += result.checked
        view = VocabularyView(inventory, budget=operation)
        for reference in usage.references:
            operation.consume("work")
            if reference.role not in kinds:
                continue
            bound_text(reference.iri, operation)
            bound_text(reference.location, operation)
            state = view.deprecation(reference.iri, budget=operation)
            retain_report(state.report, operation, issues)
            if state.state == "deprecated":
                issues.append(
                    Issue(
                        "query.vocabulary_deprecated",
                        "Query references a deprecated vocabulary term",
                        reference.location,
                        severity="warning",
                        actual=reference.iri,
                    )
                )
            elif state.state in ("conflicting", "unverified"):
                issues.append(
                    Issue(
                        "query.deprecation_unverified",
                        "Term deprecation metadata is unresolved",
                        reference.location,
                        kind="unverified",
                        actual=reference.iri,
                    )
                )
        complete = (
            usage.report.complete
            and result.complete
            and not any(issue.kind != "finding" for issue in issues)
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "query.usage",
        checked,
        tuple(issues),
        requested=max(1, checked),
        complete=complete,
        allow_empty=True,
        children=tuple(children),
        families=("query.vocabulary",),
        regime="parsed-static-dependencies",
    )
