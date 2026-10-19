"""Policy success must not erase unknown or incomplete child checks."""

import json

import pytest

from bijux_ontology.errors import InputError
from bijux_ontology.reports import (
    AcceptancePolicy,
    Issue,
    IssueSelection,
    Location,
    Report,
    combine_reports,
)


def test_unverified_child_remains_unverified_when_policy_allows_it() -> None:
    child = Report(
        "child",
        1,
        (Issue("models.unsupported", "Anonymous range", kind="unverified"),),
    )
    report = combine_reports("project", Report("vocabulary", 2), child)
    assert report.exit_code() == 0
    assert not report.verified
    assert report.exit_code(strict=True) == 1
    assert report.issues == child.issues


def test_public_report_construction_preserves_descendant_truth_without_recounting() -> (
    None
):
    from bijux_ontology.interfaces.cli.protocol import report_from_data

    failure = Issue("emission.missing", "Missing value", "/label")
    leaf = Report("emission", 1, (failure,))
    parent = Report("document", 1, children=(leaf,))
    root = Report("consumer", 1, children=(parent,))
    for report in (root, report_from_data(json.loads(json.dumps(root.to_dict())))):
        assert report.outcome == "disagreement" and not report.verified
        assert report.exit_code(strict=True) == 1
        assert report.checked == 1 and report.issues == (failure,)
    combined = combine_reports("combined", root)
    assert combined.checked == 1 and combined.issues == (failure,)
    agreeing = Report("consumer", 1, children=(Report("document", 1),))
    assert agreeing.verified and agreeing.exit_code(strict=True) == 0


def test_child_input_and_exhaustion_cannot_be_hidden_by_optional_empty_scope() -> None:
    for kind in ("input_error", "exhausted"):
        child = Report(
            "child",
            0,
            (Issue("execution.failed", "Unavailable", kind=kind),),
            allow_empty=True,
        )
        parent = Report("parent", 1, children=(child,))
        assert parent.exit_code() == 2 and not parent.complete


def test_incomplete_input_takes_precedence_without_losing_findings() -> None:
    missing = Issue("vocabulary.term_missing", "Missing term", "field")
    unavailable = Issue("input.source_missing", "Missing source", kind="input_error")
    report = combine_reports("project", Report("vocabulary", 1, (missing, unavailable)))
    assert report.exit_code() == 2
    assert report.issues == (missing, unavailable)


def test_successful_sibling_cannot_hide_an_empty_required_scope() -> None:
    report = combine_reports("project", Report("vocabulary", 2), Report("models"))
    assert report.exit_code() == 2
    assert any(issue.code == "input.empty_scope" for issue in report.issues)


def test_optional_empty_aggregate_is_not_a_required_input_failure() -> None:
    optional = combine_reports("optional project", Report("optional", allow_empty=True))
    assert optional.exit_code() == 0
    assert optional.outcome == "not_applicable"
    assert not optional.verified
    combined = combine_reports("project", optional, Report("required", 1))
    assert combined.verified
    assert combined.children == (optional, Report("required", 1))


def test_invalid_counts_and_unfinished_required_work_cannot_verify() -> None:
    with pytest.raises(InputError):
        Report("invalid", -1)
    with pytest.raises(InputError):
        Report("invalid", 2, requested=1)
    pending = Report("requested", 1, requested=2)
    assert not pending.complete
    assert not pending.verified
    assert pending.exit_code(strict=True) == 1
    assert pending.issues[0].code == "execution.incomplete"


def test_policy_acceptance_never_changes_raw_truth_or_loses_evidence() -> None:
    issue = Issue("example.conflict", "Mismatch", "model.field", severity="warning")
    report = Report("comparison", 1, (issue,))
    assert report.exit_code() == 0
    assert not report.verified
    assert report.outcome == "disagreement"
    policy = AcceptancePolicy(
        minimum_severity="warning",
        exceptions=(IssueSelection(issue.code, issue.location),),
    )
    assert report.exit_code(policy=policy) == 0
    assert report.issues == (issue,)
    unavailable = Report(
        "comparison",
        1,
        (
            Issue(
                "example.conflict", "Missing source", "model.field", kind="input_error"
            ),
        ),
    )
    assert unavailable.exit_code(policy=policy) == 2
    exhausted = Report(
        "comparison",
        1,
        (Issue("execution.limit", "Work budget exceeded", kind="exhausted"),),
    )
    assert exhausted.outcome == "exhausted"
    assert exhausted.exit_code() == 2


def test_structured_output_preserves_distinct_occurrences_and_child_scope() -> None:
    issues = tuple(
        Issue(
            "context.mismatch",
            "Wrong predicate",
            details=Location(source="document.json", path=("x/y", index)),
        )
        for index in (1, 0)
    )
    report = combine_reports(
        "project", Report("context", 2, issues, regime="jsonld"), Report("model", 1)
    )
    encoded = json.loads(json.dumps(report.to_dict()))
    assert encoded["schema_version"] == 1
    assert encoded["complete"]
    assert encoded["checked"] == encoded["requested"] == 3
    assert [row["location"] for row in encoded["issues"]] == [
        "document.json/x~1y/0",
        "document.json/x~1y/1",
    ]
    assert len(report.select_issues(code="context.mismatch")) == 2
    assert encoded["scopes"][0]["regime"] == "jsonld"


def test_excluded_scope_and_accepted_findings_remain_distinct_in_nested_reports() -> (
    None
):
    from bijux_ontology.reports import AcceptancePolicy, IssueSelection

    finding = Issue("convention.choice", "Consumer convention differs", "title")
    child = Report("mapping", 1, (finding,), exclusions=("legacy aliases",))
    report = combine_reports("project", combine_reports("document", child))
    policy = AcceptancePolicy(
        exceptions=(IssueSelection("convention.choice", "title"),)
    )
    assert report.exit_code(policy=policy) == 0
    assert not report.verified
    import json

    encoded = json.loads(json.dumps(report.to_dict(policy=policy)))
    assert encoded["exclusions"] == ["legacy aliases"]
    assert encoded["policy"]["permitted"] == [
        {"code": "convention.choice", "location": "title"}
    ]
    assert encoded["scopes"][0]["scopes"][0]["issues"][0]["code"] == "convention.choice"


def test_valid_unsupported_scope_is_unverified_rather_than_invalid_input() -> None:
    report = Report(
        "unsupported",
        0,
        (
            Issue(
                "feature.unsupported",
                "Valid input uses an unsupported feature",
                kind="unverified",
            ),
        ),
        requested=1,
        complete=False,
    )
    assert report.outcome == "unverified"
    assert report.exit_code() == 0 and report.exit_code(strict=True) == 1
    assert not any(issue.kind == "input_error" for issue in report.issues)


def test_structured_issue_evidence_is_owned_finite_and_part_of_identity() -> None:
    issue = Issue(
        "example.conflict", "Mismatch", evidence_json='{"path":"name","values":[1]}'
    )
    reordered = Issue(
        "example.conflict", "Mismatch", evidence_json='{"values":[1],"path":"name"}'
    )
    assert issue == reordered and hash(issue) == hash(reordered)
    changed = Issue(
        "example.conflict", "Mismatch", evidence_json='{"path":"age","values":[1]}'
    )
    assert len({issue, changed}) == 2
    copy = issue.evidence
    assert copy is not None
    copy["path"] = "unrelated"
    assert issue.evidence != copy
    assert issue.to_dict()["evidence"] == {"path": "name", "values": [1]}
    for invalid in ("[]", '{"value":NaN}', '{"value":Infinity}'):
        with pytest.raises(InputError):
            Issue("example.conflict", "Mismatch", evidence_json=invalid)
