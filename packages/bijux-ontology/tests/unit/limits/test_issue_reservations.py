"""Paid findings transfer without freeing repeated output from issue limits."""

import gc
import json
import tracemalloc
import weakref

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report, report_from_data
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)


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


def test_owned_worker_roundtrip_preserves_paid_nested_finding_ownership() -> None:
    """Transported child copies share ownership and do not require a second slot."""
    finding = Issue("example.invalid", "Actual completed failure")
    original = Report(
        "outer", 1, (finding,), children=(Report("inner", 1, (finding,)),)
    )
    received = report_from_data(json.loads(json.dumps(original.to_dict())))
    assert received.to_dict() == original.to_dict()
    assert received.issues[0] is received.children[0].issues[0]
    host = Budget(Limits(max_issues=1))
    host.consume("states", 16)
    host.consume("bytes", 1024)
    host.consume("work", 16)
    host.consume("issues")
    host._register_transferred_report(received, paid_issues=1, max_references=16)
    retained = IssueCollector(host)
    retain_report(received, host, retained)
    assert retained == [finding] and host.remaining("issues") == 0
    with pytest.raises(LimitExceeded):
        retained.append(Issue("example.invalid", "Actual completed failure"))


def test_invalid_worker_receipt_cannot_authorize_a_finding() -> None:
    """Missing logical payment and missing host reservation both fail closed."""
    report = Report("native", 1, (Issue("example.invalid", "Not paid here"),))
    for paid, capacity in ((0, 16), (1, 1)):
        host = Budget(Limits(max_issues=1))
        host.consume("issues")
        with pytest.raises(InputError):
            host._register_transferred_report(
                report, paid_issues=paid, max_references=capacity
            )
        with pytest.raises(LimitExceeded):
            IssueCollector(host).append(report.issues[0])


@pytest.mark.parametrize("boundary", ["issue", "term"])
def test_unicode_metadata_stops_before_allocating_its_full_encoding(
    boundary: str,
) -> None:
    """A depleted operation cannot allocate an oversized diagnostic or RDF term."""
    text = "🧬" * 400000
    finding = Issue("literal.invalid", text)
    budget = Budget(Limits(max_total_bytes=32768 if boundary == "issue" else 500000))
    tracemalloc.start()
    try:
        with pytest.raises(LimitExceeded):
            if boundary == "issue":
                IssueCollector(budget).append(finding)
            else:
                bound_text(text, budget)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100000
    valid = Budget(Limits(max_bytes=4, max_total_bytes=4))
    bound_text("🧬", valid)
    assert valid.remaining("bytes") == 0


def test_nested_storage_reservations_preserve_total_and_wire_evidence_bounds() -> None:
    """Unexported copies cannot evade total bytes or spend diagnostic headroom."""
    from bijux_ontology.workflows.projects.representations.worker import (
        _PublicationBudget,
    )

    limits = Limits(max_total_bytes=128)
    host = Budget(limits)
    allowances = {
        kind: host.remaining(kind)
        for kind in ("bytes", "work", "states", "issues", "resources", "triples")
    }
    worker = _PublicationBudget(limits, allowances, 8)
    outer = Operation(None, worker, 10, None)
    nested = Operation(None, outer, 10, None)
    nested.reserve_storage(100)
    nested.consume("bytes", 8)
    assert worker.consumed()["bytes"] == 108
    assert nested.remaining("bytes") == 20
    with pytest.raises(LimitExceeded, match="evidence allowance"):
        nested.consume("bytes")
    with pytest.raises(LimitExceeded, match="shared allowance"):
        nested.reserve_storage(21)
    with pytest.raises(InputError):
        nested.reserve_storage(-1)
    nested.reserve_storage(20)
    assert worker.consumed()["bytes"] == 128
