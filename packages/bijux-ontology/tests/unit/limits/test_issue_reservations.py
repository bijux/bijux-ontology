"""Paid findings transfer without freeing repeated output from issue limits."""

import gc
import weakref

import pytest

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.reports import Issue, Location, Report


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


def test_late_report_adoption_keeps_only_its_previously_qualified_findings() -> None:
    budget = Budget()
    native = IssueCollector(Operation(None, budget, 10, None))
    unrelated = Issue("example.before", "An earlier unrelated scope failed")
    finding = Issue("example.current", "This returned native check failed")
    native.extend((unrelated, finding))
    returned = Report("current", 1, (finding,))
    budget.consume("work", budget.remaining("work"))
    parent = IssueCollector(budget)
    with pytest.raises(LimitExceeded):
        retain_report(returned, budget, parent)
    assert parent == [finding] and parent[0] is finding
    with pytest.raises(LimitExceeded):
        retain_report(returned, budget, parent)
    assert parent == [finding]
    parent.exhausted("Further report metadata could not be adopted")
    assert [issue.code for issue in parent] == [
        "example.current",
        "execution.limit_exceeded",
    ]


def test_partial_metadata_qualification_cannot_authorize_unbounded_issue_handoff() -> (
    None
):
    budget = Budget(Limits(max_bytes=64))
    native = IssueCollector(budget)
    unqualified = Issue(
        "example.invalid", "Bounded message", details=Location(source="x" * 65)
    )
    with pytest.raises(LimitExceeded):
        native.append(unqualified)
    assert not native
    budget.consume("work", budget.remaining("work"))
    parent = IssueCollector(budget)
    with pytest.raises(LimitExceeded):
        retain_report(Report("returned", 1, (unqualified,)), budget, parent)
    assert not parent
