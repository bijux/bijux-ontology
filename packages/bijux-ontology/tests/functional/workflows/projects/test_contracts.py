"""Required consumer runs preserve coverage, descendant failures and narrow policy."""

import json
from dataclasses import replace

import pytest

from bijux_ontology.declarations.bindings import (
    BindingIdentity,
    BindingObservation,
    ContractEvidence,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import (
    AcceptancePolicy,
    CheckError,
    Issue,
    IssueSelection,
    Report,
    combine_reports,
)
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.usages.checking import check_context
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    ContractResult,
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


def test_required_identities_use_actual_two_sample_coverage_not_counts() -> None:
    required = (
        BindingIdentity("Study", ("title",)),
        BindingIdentity("Study", ("abstract",)),
    )

    def observe(document: dict[str, object]) -> tuple[BindingObservation, ...]:
        return tuple(
            BindingObservation(
                BindingIdentity("Study", (key,)),
                check_context(document, (ContextExpectation((key,), "urn:" + key),)),
            )
            for key in document
            if key != "@context"
        )

    title = observe({"@context": {"@vocab": "urn:"}, "title": "Study"})
    abstract = observe({"@context": {"@vocab": "urn:"}, "abstract": "Overview"})

    def contract(observations: tuple[BindingObservation, ...]) -> ContractResult:
        raw = combine_reports("samples", *(item.report for item in observations))
        return check_contract(
            (
                ContractObligation(
                    "output",
                    lambda: ContractEvidence(raw, observations),
                    bindings=len(observations),
                    minimum_bindings=2,
                    required_bindings=required,
                ),
            )
        )

    repeated = contract(title + title)
    assert not repeated.accepted and not repeated.report.verified
    assert any(
        issue.code == "contract.binding_missing" for issue in repeated.report.issues
    )
    completed = contract(title + abstract)
    assert completed.accepted and completed.require_verified().verified
    assert completed.observed_bindings == (("output", title + abstract),)


def test_unverified_and_excluded_bindings_never_satisfy_required_coverage() -> None:
    identity = BindingIdentity("Study", ("abstract",))
    unknown = Report(
        "association",
        1,
        (Issue("association.unknown", "No proved correspondence", kind="unverified"),),
    )
    for observation in (
        BindingObservation(identity, unknown),
        BindingObservation(
            identity, Report("sample", 1), exclusion="Unmapped extension"
        ),
    ):

        def check(current: BindingObservation = observation) -> ContractEvidence:
            return ContractEvidence(Report("sample", 1), (current,))

        result = check_contract(
            (
                ContractObligation(
                    "output",
                    check,
                    required_bindings=(identity,),
                ),
            )
        )
        assert not result.accepted
        assert any(
            issue.code == "contract.binding_missing" for issue in result.report.issues
        )
        assert result.observed_bindings == (("output", (observation,)),)
