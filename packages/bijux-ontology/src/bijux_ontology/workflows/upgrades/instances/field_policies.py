"""Selected instance field acceptance, independent of exact release identity."""

from rdflib import Literal

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.records.types import PatchLiteral, PatchValue
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.rdf.value_comparison.comparing import compare_literal_values
from bijux_ontology.rdf.value_comparison.types import LiteralValueComparison

from .types import InstanceFieldPolicy


def _reserve(budget: Budget, count: int = 1) -> None:
    """Prepay owned result/container references before constructing them."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)


def _literal(value: PatchLiteral) -> Literal:
    """Use the existing lexical-preserving constructor for native value comparison."""
    return literal_term(
        value.lexical,
        datatype=None if value.language else value.datatype,
        language=value.language,
    )


def _relation(
    left: PatchValue,
    right: PatchValue,
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Keep exact identity distinct from caller-selected literal value equality."""
    budget.consume("work")
    if (
        policy.equality == "rdf-term"
        or not isinstance(left, PatchLiteral)
        or not isinstance(right, PatchLiteral)
    ):
        return left == right
    _reserve(budget)
    size = sum(
        len(text)
        for value in (left, right)
        for text in (value.lexical, value.datatype, value.language or "")
    )
    budget.consume("work", size)
    budget.consume("bytes", 32 * size + 128)
    comparison = compare_literal_values(
        _literal(left),
        _literal(right),
        regime=policy.equality,
        budget=budget,
    )
    issues.retain_completed(comparison.report)
    proofs.append(comparison)
    retain_report(comparison.report, budget, issues)
    return (
        True
        if comparison.relation == "equal"
        else False
        if comparison.relation == "different"
        else None
    )


def _included(
    left: tuple[PatchValue, ...],
    right: tuple[PatchValue, ...],
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Establish set inclusion without choosing a greedy value pairing."""
    unknown = False
    for value in left:
        found = False
        uncertain = False
        candidates = IssueCollector(budget)
        try:
            for candidate in right:
                result = _relation(value, candidate, policy, budget, candidates, proofs)
                if any(issue.kind == "exhausted" for issue in candidates):
                    raise LimitExceeded(
                        "Instance value comparison exhausted its allowance"
                    )
                if result is True:
                    found = True
                    break
                uncertain |= result is None
        except LimitExceeded:
            issues.extend(candidates)
            raise
        if not found:
            # Inclusion needs one qualified witness, not comparison of every
            # unrelated operand family. Keep all raw probes in comparisons.
            issues.extend(candidates)
            if not uncertain:
                return False
            unknown = True
    return None if unknown else True


def accept_field(
    before: tuple[PatchValue, ...],
    after: tuple[PatchValue, ...],
    policy: InstanceFieldPolicy,
    budget: Budget,
    issues: IssueCollector,
    proofs: list[LiteralValueComparison],
) -> bool | None:
    """Apply finite ordered-prefix or set semantics under the named equality regime."""
    if policy.change == "allow":
        return True
    if policy.collection == "list":
        if (
            len(before) > len(after)
            or policy.change == "immutable"
            and len(before) != len(after)
        ):
            return False
        unknown = False
        for left, right in zip(before, after, strict=False):
            equal = _relation(left, right, policy, budget, issues, proofs)
            if equal is False:
                return False
            unknown |= equal is None
        return None if unknown else True
    included = _included(before, after, policy, budget, issues, proofs)
    if policy.change == "append-only" or included is False:
        return included
    reverse = _included(after, before, policy, budget, issues, proofs)
    return (
        False
        if reverse is False
        else None
        if included is None or reverse is None
        else True
    )
