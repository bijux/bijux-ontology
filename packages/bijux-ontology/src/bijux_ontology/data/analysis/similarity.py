"""Base-two corpus Resnik similarity with complete pair and set evidence."""

import math
from collections.abc import Sequence
from threading import Event

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.vocabulary.relation_paths.automaton import named

from .population import capture, ordered, qualify, reserve
from .types import (
    AncestorInformation,
    AnnotationPopulation,
    TermSetSimilarity,
    TermSimilarity,
)


def _pair(
    population: AnnotationPopulation,
    left: URIRef,
    right: URIRef,
    operation: Budget,
) -> TermSimilarity:
    """Calculate one required pair without dropping unsupported term coverage."""
    issues = IssueCollector(operation)
    a = b = None
    score = None
    witnesses: list[AncestorInformation] = []
    complete = False
    try:
        if type(left) is not URIRef or type(right) is not URIRef:
            raise InputError("Similarity terms require exact named URIRef identities")
        a, b = named(left, operation), named(right, operation)
        operation.consume("work", len(left) + len(right) + 2)
        if a not in population.ancestors or b not in population.ancestors:
            issues.append(
                Issue(
                    "analysis.term_unknown",
                    "Required similarity term is outside the qualified population",
                    kind="unverified",
                )
            )
        elif not population.members[a] or not population.members[b]:
            issues.append(
                Issue(
                    "analysis.zero_frequency",
                    "Required similarity term has no supplied eligible annotations",
                    kind="unverified",
                )
            )
        else:
            reserve(
                operation, len(population.ancestors[a]) + len(population.ancestors[b])
            )
            shared = population.ancestors[a].intersection(population.ancestors[b])
            if not shared:
                issues.append(
                    Issue(
                        "analysis.disconnected",
                        "Required terms have no selected shared ancestor",
                        kind="unverified",
                    )
                )
            else:
                best_count = len(population.background) + 1
                for ancestor in ordered(shared, operation):
                    operation.consume("work")
                    count = len(population.members[ancestor])
                    if not count:
                        issues.append(
                            Issue(
                                "analysis.zero_frequency",
                                "Shared ancestor has no qualified positive frequency",
                                str(ancestor),
                                kind="unverified",
                            )
                        )
                        break
                    if count <= best_count:
                        reserve(operation)
                        information = -math.log2(count / len(population.background))
                        if count < best_count:
                            witnesses.clear()
                        witnesses.append(
                            AncestorInformation(
                                ancestor, count, len(population.background), information
                            )
                        )
                        best_count = count
                else:
                    complete = True
                    score = witnesses[0].information
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("analysis.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))

    report = Report(
        "analysis.resnik_pair",
        checked=int(complete),
        requested=1,
        complete=complete,
        issues=tuple(issues),
    )
    return TermSimilarity(
        a,
        b,
        score if report.verified else None,
        tuple(witnesses),
        population.identity,
        report,
    )


def resnik_similarity(
    population: AnnotationPopulation,
    left: URIRef,
    right: URIRef,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> TermSimilarity:
    """Return max shared-ancestor information, not OWL equivalence or confidence."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    try:
        qualify(population, operation, issues)
        reserve(operation)
        return _pair(population, left, right, operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("analysis.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))

    return TermSimilarity(
        None,
        None,
        None,
        (),
        None,
        Report(
            "analysis.resnik_pair", requested=1, complete=False, issues=tuple(issues)
        ),
    )


def resnik_term_sets(
    population: AnnotationPopulation,
    left: Sequence[URIRef],
    right: Sequence[URIRef],
    *,
    aggregation: str = "symmetric-mean-directional-best",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> TermSetSimilarity:
    """Average the two directional mean-best scores, requiring every pair.

    Duplicate terms carry no extra weight. All tied best-match pair identities
    remain available alongside the full matrix. A missing pair withholds the
    aggregate; disconnected terms cannot silently disappear from either set.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    pairs: list[TermSimilarity] = []
    a: tuple[URIRef, ...] = ()
    b: tuple[URIRef, ...] = ()
    left_best: list[tuple[URIRef, URIRef]] = []
    right_best: list[tuple[URIRef, URIRef]] = []
    score = None
    identity = None
    requested = 1
    complete = False
    try:
        qualify(population, operation, issues)
        identity = population.identity
        if (
            type(aggregation) is not str
            or aggregation != "symmetric-mean-directional-best"
        ):
            raise InputError("Unsupported term-set aggregation")
        captured = []
        for values in (left, right):
            selected = set()
            for term in capture(values, URIRef, operation):
                named(term, operation)
                reserve(operation)
                selected.add(term)
            if not selected:
                raise InputError("Term-set similarity requires two nonempty term sets")
            captured.append(ordered(selected, operation))
        a, b = captured
        requested = len(a) * len(b)
        reserve(operation, requested * 3)
        matrix: dict[tuple[URIRef, URIRef], float] = {}
        for first in a:
            for second in b:
                result = _pair(population, first, second, operation)
                pairs.append(result)
                if result.report.verified and result.score is not None:
                    matrix[first, second] = result.score
        if len(matrix) == requested:
            reserve(operation, requested * 2 + len(a) + len(b))
            left_values: list[float] = []
            right_values: list[float] = []
            for source, targets, inverse, scores, best in (
                (a, b, False, left_values, left_best),
                (b, a, True, right_values, right_best),
            ):
                for term in source:
                    operation.consume("work", len(targets))
                    value = max(
                        matrix[(other, term) if inverse else (term, other)]
                        for other in targets
                    )
                    scores.append(value)
                    for other in targets:
                        if matrix[(other, term) if inverse else (term, other)] == value:
                            best.append((term, other))
            operation.consume("work", len(left_values) + len(right_values))
            score = 0.5 * (
                math.fsum(left_values) / len(a) + math.fsum(right_values) / len(b)
            )
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("analysis.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))

    report = Report(
        "analysis.resnik_sets",
        checked=len(pairs),
        requested=requested,
        complete=complete,
        issues=tuple(issues),
        children=tuple(pair.report for pair in pairs),
    )
    return TermSetSimilarity(
        a,
        b,
        score if report.verified else None,
        tuple(pairs),
        tuple(left_best),
        tuple(right_best),
        identity,
        report,
    )
