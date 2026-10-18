"""Paid findings transfer without freeing repeated output from issue limits."""

import gc
import weakref

import pytest

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.reports import Issue


def test_nested_transfers_survive_full_budget_but_local_repetition_still_costs() -> (
    None
):
    budget = Budget(Limits(max_issues=2))
    child, parent, outer = (IssueCollector(budget) for _ in range(3))
    finding = Issue("literal.lexical_invalid", "Known false")
    child.append(finding)
    parent.extend(child)
    parent.append(finding)
    assert parent == [finding, finding]
    with pytest.raises(LimitExceeded):
        parent.append(finding)
    outer.append(finding)
    assert outer[0] is finding
    with pytest.raises(LimitExceeded):
        outer.append(Issue("literal.datatype_unverified", "Pending scope"))


def test_equal_new_findings_and_independent_budgets_remain_separately_charged() -> None:
    finding = Issue("literal.lexical_invalid", "Same text")
    first = IssueCollector(Budget(Limits(max_issues=1)))
    second = IssueCollector(Budget(Limits(max_issues=1)))
    first.append(finding)
    second.append(finding)
    for collector in (first, second):
        with pytest.raises(LimitExceeded):
            collector.append(Issue("literal.lexical_invalid", "Same text"))


def test_reservation_keeps_issue_alive_after_child_collection_releases_it() -> None:
    budget = Budget(Limits(max_issues=1))
    child = IssueCollector(budget)
    finding = Issue("literal.lexical_invalid", "Owned evidence")
    reference = weakref.ref(finding)
    child.append(finding)
    child.clear()
    del finding
    gc.collect()
    retained = reference()
    assert retained is not None
    parent = IssueCollector(budget)
    parent.append(retained)
    assert parent[0] is retained
