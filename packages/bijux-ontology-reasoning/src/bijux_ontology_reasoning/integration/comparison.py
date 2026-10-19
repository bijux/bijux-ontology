"""Finite before/after classification without inferring unobserved logical truth."""

from rdflib import URIRef

from bijux_ontology.errors import InputError
from bijux_ontology.limits import IssueCollector
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.reports import Issue, Report

from .preparation import reserve
from .types import EntailmentComparison, TheoryComparison, TheoryObservation


def _answers(
    observation: TheoryObservation, operation: Operation
) -> dict[URIRef, bool]:
    """Index only actual paid native named-class answers."""
    values = observation.consistency.classes
    reserve(operation, len(values) + 1)
    return {answer.iri: answer.satisfiable for answer in values}


def _entailed(observation: TheoryObservation, index: int) -> bool | None:
    """Return a qualified answer; raw native booleans alone do not prove it."""
    result = observation.entailments
    if result is None or index >= len(result.answers):
        return None
    answer = result.answers[index]
    if not answer.report.complete:
        return None
    return (
        True
        if answer.outcome == "entailed"
        else False
        if answer.outcome == "not_entailed"
        else None
    )


def compare_theories(
    before: tuple[TheoryObservation, ...],
    after: TheoryObservation,
    operation: Operation,
) -> TheoryComparison:
    """Compare independently qualified originals or an unbridged joint baseline.

    Signature absence is an absence of a pre-existing named-class problem, not
    a fabricated satisfiability query. Only complete native inventories can
    establish that absence. Inconsistent or unknown baselines cannot establish
    a new interaction. Positive entailment changes never imply negated axioms.
    This helper is shared by ontology composition and explicitly authored bridges.
    """
    if not before:
        raise InputError("A theory comparison requires explicit baseline observations")
    reserve(operation, len(before) + len(after.consumer_entailments) + 8)
    issues = IssueCollector(operation)
    prior = tuple(value.consistency.consistent for value in before)
    current = after.consistency.consistent
    new_inconsistent = None
    if current is True or any(value is False for value in prior):
        new_inconsistent = False
    elif current is False and all(value is True for value in prior):
        new_inconsistent = True
    previous = tuple(_answers(value, operation) for value in before)
    actual = _answers(after, operation)
    reserve(operation, len(actual) + 1)
    selected = tuple(actual) if after.named_classes is None else after.named_classes
    newly: list[URIRef] = []
    existing: list[URIRef] = []
    complete = (
        current is not None
        and all(value is not None for value in prior)
        and after.report.complete
        and all(value.report.complete for value in before)
    )
    for iri in selected:
        operation.consume("work", len(before) + 1)
        value = actual.get(iri)
        old = tuple(answers.get(iri) for answers in previous)
        if value is False and any(answer is False for answer in old):
            existing.append(iri)
        elif value is False:
            if (
                current is True
                and all(value.consistency.consistent is True for value in before)
                and all(value.signature_complete for value in before)
            ):
                newly.append(iri)
            else:
                complete = False
        elif value is None and current is not False:
            complete = False
    queries = []
    for index, axiom in enumerate(after.consumer_entailments):
        operation.consume("work", len(before) + 1)
        old_answers = tuple(
            _entailed(value, index)
            if value.consumer_entailments == after.consumer_entailments
            else None
            for value in before
        )
        now = _entailed(after, index)
        queries.append(EntailmentComparison(axiom, old_answers, now))
        if now is None or any(answer is None for answer in old_answers):
            complete = False
    if not complete:
        issues.append(
            Issue(
                "owl.integration_comparison_unverified",
                "A required prior or joint logical observation is unknown",
                after.label,
                kind="unverified",
            )
        )
    return TheoryComparison(
        tuple(value.label for value in before),
        after.label,
        new_inconsistent,
        tuple(newly),
        tuple(existing),
        tuple(queries),
        Report(
            "owl.theory_comparison",
            int(complete),
            tuple(issues),
            requested=1,
            complete=complete,
            regime="qualified-selected-prior-versus-joint-observations",
        ),
    )
