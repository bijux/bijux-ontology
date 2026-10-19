"""Publisher promises remain optional and use actual protected/extension probes."""

from dataclasses import replace

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.contracts.cases import ContextCase
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.jsonld.publication import (
    ContextOverride,
    ContextPublication,
    PublicationContract,
    PublishedTerm,
    check_context_publication,
)
from bijux_ontology.jsonld.usages.checking import check_context


def _fixture() -> tuple[dict[str, object], PublicationContract]:
    """Declare one protected retained field and one real extension point."""
    context: dict[str, object] = {
        "@version": 1.1,
        "@protected": True,
        "name": "urn:name",
        "extension": {"@id": "urn:extension", "@protected": False},
    }
    retained = ContextCase(
        "retained",
        {"name": "Text"},
        (ContextExpectation(("name",), "urn:name", value_kind="literal"),),
    )
    extension = ContextCase(
        "extension",
        {"extension": "More"},
        (ContextExpectation(("extension",), "urn:changed", value_kind="literal"),),
    )
    contract = PublicationContract(
        (PublishedTerm("name", True), PublishedTerm("extension", False)),
        (retained,),
        (
            ContextOverride("guard", "name", "urn:changed", "blocked"),
            ContextOverride(
                "extend", "extension", "urn:changed", "allowed", (extension,)
            ),
        ),
        required_version="1.1",
        vocabulary=None,
        effective_base=None,
    )
    return context, contract


def _reported(result: ContextPublication) -> set[int]:
    """Expected native rejection remains raw evidence outside required policy truth."""
    pending: list[Report] = [result.report]
    pending.extend(result.cases)
    if result.inspection is not None:
        pending.append(result.inspection.report)
    for probe in result.overrides:
        pending.extend(probe.cases)
        for inspection in (probe.replacement, probe.inspection):
            if inspection is not None:
                pending.append(inspection.report)
        if probe.report is not None:
            pending.append(probe.report)
    identities: set[int] = set()
    while pending:
        report = pending.pop()
        identities.update(id(issue) for issue in report.issues)
        pending.extend(report.children)
    return identities


def test_effective_protection_and_actual_legal_extension_keep_raw_truth() -> None:
    context, contract = _fixture()
    result = check_context_publication(context, contract)
    assert result.report.verified and result.inspection is not None
    assert len(result.overrides) == 2
    guard, extension = result.overrides
    assert guard.report and guard.report.verified and guard.inspection
    assert guard.inspection.report.outcome == "input_error"
    assert guard.inspection.report.issues[0].actual == "protected term redefinition"
    assert extension.report and extension.report.verified and extension.inspection
    assert all(report.verified for report in extension.cases)
    original = result.context
    assert isinstance(original, dict)
    original.clear()
    assert result.context
    assert result.inspection.view is not None
    assert any(
        term.term == "name" and term.protected for term in result.inspection.view.terms
    )


def test_removed_protection_or_failed_retained_contract_cannot_pass_policy() -> None:
    context, contract = _fixture()
    del context["@protected"]
    ordinary = check_context(
        {"@context": context, "name": "Text"}, contract.cases[0].expectations
    )
    assert ordinary.verified
    policy = check_context_publication(context, contract)
    assert policy.report.outcome == "disagreement"
    assert any(
        issue.code == "context.publication_mismatch" for issue in policy.report.issues
    )
    context, contract = _fixture()
    failed = replace(
        contract,
        cases=(
            replace(
                contract.cases[0],
                expectations=(ContextExpectation(("name",), "urn:expected"),),
            ),
        ),
    )
    checked = check_context_publication(context, failed)
    assert checked.report.outcome == "disagreement"
    assert checked.overrides[0].report and checked.overrides[0].report.verified
    assert any(
        issue.code == "context.mapping_mismatch" for issue in checked.report.issues
    )


def test_declared_local_version_and_effective_defaults_are_distinct_requirements() -> (
    None
):
    context, contract = _fixture()
    del context["@version"]
    absent = check_context_publication(context, contract)
    assert absent.report.outcome == "disagreement"
    assert any(issue.location == "@version" for issue in absent.report.issues)
    assert check_context_publication(
        context, replace(contract, required_version=None)
    ).report.verified
    context, contract = _fixture()
    for key, value in (
        ("@vocab", "urn:vocabulary:"),
        ("@base", "https://example.org/new/"),
    ):
        changed = {**context, key: value}
        selected_contract = (
            replace(contract, effective_base="https://example.org/input/")
            if key == "@base"
            else contract
        )
        result = check_context_publication(
            changed,
            selected_contract,
            base="https://example.org/input/" if key == "@base" else None,
        )
        assert result.report.outcome == "disagreement"
        assert any(issue.location == key for issue in result.report.issues)
    loader = OfflineDocumentLoader({"urn:context": {"@context": context}})
    for raw in ("urn:context", [context]):
        unknown = check_context_publication(raw, contract, document_loader=loader)
        assert unknown.report.outcome == "unverified"
        assert any(
            issue.code == "context.publication_unverified"
            for issue in unknown.report.issues
        )
        assert not any(issue.location == "@version" for issue in unknown.report.issues)
    imported = {"@import": "urn:context"}
    without_version = {
        key: value for key, value in context.items() if key != "@version"
    }
    import_loader = OfflineDocumentLoader(
        {"urn:context": {"@context": without_version}}
    )
    assert (
        check_context_publication(
            imported, contract, document_loader=import_loader
        ).report.outcome
        == "unverified"
    )


def test_unexercised_malformed_or_scoped_override_is_not_a_protection_proof() -> None:
    context, contract = _fixture()
    for replacement in (17, {"@id": 17}):
        probes = (
            replace(contract.overrides[0], definition=replacement),
            contract.overrides[1],
        )
        result = check_context_publication(context, replace(contract, overrides=probes))
        assert not result.report.verified
        assert (
            result.overrides[0].replacement
            and not result.overrides[0].replacement.report.verified
        )
    same = replace(contract.overrides[1], definition="urn:extension")
    idle = check_context_publication(
        context, replace(contract, overrides=(contract.overrides[0], same))
    )
    assert idle.report.outcome == "unverified"
    scoped = {
        **context,
        "name": {"@id": "urn:name", "@context": {"inner": "urn:inner"}},
    }
    assert check_context_publication(scoped, contract).report.outcome == "unverified"
    for invalid in (
        replace(contract, overrides=()),
        replace(contract, cases=()),
        replace(
            contract,
            overrides=(contract.overrides[0], replace(contract.overrides[1], cases=())),
        ),
    ):
        assert (
            check_context_publication(context, invalid).report.outcome == "input_error"
        )


def test_cumulative_bounds_keep_paid_native_and_policy_evidence() -> None:
    context, contract = _fixture()
    huge = replace(contract, terms=(PublishedTerm("x" * 100_000, True),))
    result = check_context_publication(context, huge, limits=Limits(max_bytes=1000))
    assert result.report.outcome == "exhausted" and result.overrides == ()
    measured = Budget()
    assert check_context_publication(context, contract, budget=measured).report.verified
    spent = measured.limits.max_work - measured.remaining("work")
    raw_prefixes = 0
    for cap in range(max(1, spent - 1200), spent, 97):
        budget = Budget(Limits(max_work=cap))
        result = check_context_publication(context, contract, budget=budget)
        paid = set(budget._retained_issues)
        assert paid <= _reported(result)
        assert result.report.outcome == "exhausted"
        if paid:
            raw_prefixes += 1
            assert result.overrides[0].inspection is not None
    assert raw_prefixes
    spent_issues = Budget(Limits(max_issues=1))
    spent_issues.consume("issues")
    assert (
        check_context_publication(context, contract, budget=spent_issues).report.outcome
        == "exhausted"
    )
    assert check_context_publication(context, contract).report.verified
