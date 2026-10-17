"""Required consumer runs preserve coverage, descendant failures and narrow policy."""

import json
from dataclasses import replace

import pytest

from bijux_ontology import (
    AcceptancePolicy,
    CheckError,
    ContractObligation,
    InputError,
    Issue,
    IssueSelection,
    Report,
    check_contract,
)


def test_required_bindings_and_optional_absence_do_not_use_legacy_exit_success() -> (
    None
):
    extraction = Report("models", 25, allow_empty=True)
    assert extraction.exit_code() == 0
    result = check_contract(
        (
            ContractObligation(
                "mapping", lambda: extraction, bindings=0, minimum_bindings=1
            ),
            ContractObligation("optional", None, required=False),
        )
    )
    assert not result.accepted and not result.report.verified
    assert result.raw_reports[0] == ("mapping", extraction)
    assert result.report.children[1].outcome == "not_applicable"
    with pytest.raises(CheckError):
        result.require_verified()
    passing = check_contract(
        (
            ContractObligation(
                "mapping", lambda: extraction, bindings=100, minimum_bindings=1
            ),
            ContractObligation("optional", None, required=False),
        )
    )
    assert passing.accepted and passing.require_verified().verified


def test_failing_descendants_and_unavailable_checks_do_not_hide_behind_siblings() -> (
    None
):
    failing = Report(
        "public parent",
        1,
        children=(
            Report(
                "grandchild",
                1,
                (Issue("output.missing", "Missing value", "/value"),),
            ),
        ),
    )
    result = check_contract(
        (
            ContractObligation("extraction", lambda: Report("mapped", 1)),
            ContractObligation("emission", lambda: failing),
        )
    )
    assert not result.accepted and result.report.outcome == "disagreement"
    assert result.raw_reports[1][1] is failing

    def unavailable() -> Report:
        raise InputError("Missing selected resource")

    incomplete = check_contract(
        (
            ContractObligation("resource", unavailable),
            ContractObligation("independent", lambda: Report("complete", 1)),
        )
    )
    assert not incomplete.accepted and incomplete.report.exit_code() == 2
    assert incomplete.raw_reports[1][1].verified


def test_exact_approval_cannot_waive_new_findings_at_the_same_location() -> None:
    original = Issue("output.mismatch", "Wrong predicate", "/value", expected="urn:p")
    current = [original]

    def output() -> Report:
        return Report("output", 1, tuple(current))

    obligations = (ContractObligation("emission", output),)
    initial = check_contract(obligations)
    approved = IssueSelection.for_issue(initial.report.issues[0])
    policy = AcceptancePolicy(exceptions=(approved,))
    accepted = check_contract(obligations, policy=policy)
    assert accepted.accepted and not accepted.report.verified
    encoded = json.loads(json.dumps(accepted.report.to_dict(policy=policy)))
    assert encoded["policy"]["exceptions"][0]["issue"]
    with pytest.raises(CheckError):
        accepted.require_verified()
    current.append(replace(original, expected="urn:q"))
    assert not check_contract(obligations, policy=policy).accepted
    with pytest.raises(InputError, match="exact issues"):
        check_contract(
            obligations,
            policy=AcceptancePolicy(
                exceptions=(IssueSelection(approved.code, approved.location),),
            ),
        )


def test_unverified_execution_is_strict_and_names_are_validated_before_running() -> (
    None
):
    unknown = Report(
        "unsupported", 1, (Issue("unsupported", "Unknown", kind="unverified"),)
    )
    assert unknown.exit_code() == 0
    assert not check_contract(
        (ContractObligation("required", lambda: unknown),)
    ).accepted
    with pytest.raises(InputError, match="unique"):
        check_contract(
            (ContractObligation("same", None), ContractObligation("same", None))
        )
    assert not check_contract((ContractObligation("missing", None),)).accepted
