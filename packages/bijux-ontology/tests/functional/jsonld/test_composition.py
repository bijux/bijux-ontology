"""Actual ordered context processing preserves every retained module contract."""

from dataclasses import replace

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.composition import (
    ContextCase,
    ContextModule,
    compose_contexts,
)
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.jsonld.usages.checking import check_context


def _module(name: str, predicate: str, *, iri: bool = False) -> ContextModule:
    """Declare the actual independent contract, rather than a merged dictionary."""
    mapping: object = {"@id": predicate, "@type": "@id"} if iri else predicate
    return ContextModule(
        name,
        {name: mapping},
        (
            ContextCase(
                "sample",
                {name: "urn:value" if iri else "Text"},
                (
                    ContextExpectation(
                        (name,), predicate, value_kind="iri" if iri else "literal"
                    ),
                ),
            ),
        ),
    )


def _issues(report: Report) -> set[int]:
    """Include raw child diagnostics when checking cumulative paid handoff."""
    result = {id(issue) for issue in report.issues}
    for child in report.children:
        result.update(_issues(child))
    return result


def test_compatible_modules_keep_actual_independent_contracts_and_order() -> None:
    modules = (
        _module("name", "urn:name"),
        _module("reference", "urn:reference", iri=True),
    )
    result = compose_contexts(modules, resolution="retain-required")
    assert result.report.verified and result.context_json is not None
    assert result.order == ("name", "reference")
    assert len(result.cases) == 2 and all(
        item.independent
        and item.independent.verified
        and item.composite
        and item.composite.verified
        for item in result.cases
    )
    assert check_context(
        {"@context": result.context, "name": "Other"}, modules[0].cases[0].expectations
    ).verified
    exported = result.context
    assert isinstance(exported, list)
    exported.clear()
    retained = result.context
    assert isinstance(retained, list) and len(retained) == 2


def test_mapping_coercion_null_and_protected_override_never_overwrite_contracts() -> (
    None
):
    earlier = _module("name", "urn:name")
    later = replace(_module("name", "urn:changed"), name="replacement")
    changed = compose_contexts((earlier, later), resolution="retain-required")
    assert changed.context_json is None and changed.report.outcome == "disagreement"
    assert changed.cases[0].independent and changed.cases[0].independent.verified
    assert any(
        issue.code == "context.mapping_mismatch" for issue in changed.report.issues
    )
    earlier_iri = _module("reference", "urn:reference", iri=True)
    later_literal = replace(_module("reference", "urn:reference"), name="literal")
    changed_type = compose_contexts(
        (earlier_iri, later_literal), resolution="retain-required"
    )
    assert not changed_type.report.verified and changed_type.context_json is None
    assert any(
        issue.code == "context.value_kind_mismatch"
        for issue in changed_type.report.issues
    )
    null = ContextModule(
        "reset",
        None,
        (
            ContextCase(
                "absolute",
                {"urn:extra": "Text"},
                (ContextExpectation(("urn:extra",), "urn:extra"),),
            ),
        ),
    )
    nullified = compose_contexts((earlier, null), resolution="retain-required")
    assert not nullified.report.verified and nullified.context_json is None
    protected = replace(earlier, context={"@protected": True, "name": "urn:name"})
    illegal = compose_contexts((protected, later), resolution="retain-required")
    assert illegal.report.outcome == "input_error" and illegal.context_json is None
    assert illegal.inspection and not illegal.inspection.report.verified


def test_nested_scopes_are_actual_and_root_context_cannot_insulate_a_case() -> None:
    scoped = ContextModule(
        "nested",
        {
            "inside": {"@id": "urn:inside", "@context": {"name": "urn:inner"}},
        },
        (
            ContextCase(
                "sample",
                {"inside": {"name": "Inner"}},
                (
                    ContextExpectation(
                        ("inside", "name"), "urn:inner", value_kind="literal"
                    ),
                ),
            ),
        ),
    )
    result = compose_contexts(
        (scoped, _module("name", "urn:outer")), resolution="retain-required"
    )
    assert result.report.verified
    insulated = replace(
        scoped,
        cases=(
            ContextCase(
                "bad",
                {
                    "@context": {"name": "urn:inner"},
                    "name": "Text",
                },
                (ContextExpectation(("name",), "urn:inner"),),
            ),
        ),
    )
    rejected = compose_contexts((insulated,), resolution="retain-required")
    assert rejected.context_json is None and rejected.report.outcome == "input_error"
    missing = replace(
        scoped,
        cases=(
            ContextCase(
                "missing", {}, (ContextExpectation(("inside", "name"), "urn:inner"),)
            ),
        ),
    )
    unexercised = compose_contexts((missing,), resolution="retain-required")
    assert not unexercised.report.verified and unexercised.context_json is None


def test_owned_inputs_and_remote_resources_do_not_change_during_loader_calls() -> None:
    context = {"other": "urn:other"}
    sample = {"other": "Original"}
    later = ContextModule(
        "other",
        context,
        (
            ContextCase(
                "sample",
                sample,
                (ContextExpectation(("other",), "urn:other", value_kind="literal"),),
            ),
        ),
    )
    remote = replace(_module("name", "urn:name"), context="urn:remote")
    loader = OfflineDocumentLoader({"urn:remote": {"@context": {"name": "urn:name"}}})
    calls = []

    def load(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        context["other"] = "urn:wrong"
        sample["other"] = "Mutated"
        return loader(url, options)

    result = compose_contexts(
        (remote, later), resolution="retain-required", document_loader=load
    )
    assert result.report.verified and calls == ["urn:remote"]
    retained = result.context
    assert isinstance(retained, list) and retained[1] == {"other": "urn:other"}
    absent = compose_contexts((remote,), resolution="retain-required")
    assert not absent.report.verified and absent.context_json is None

    def oversized(url: str, options: dict[str, object]) -> dict[str, object]:
        raise InputError("x" * 100_000)

    stopped = compose_contexts(
        (remote,),
        resolution="retain-required",
        document_loader=oversized,
        limits=Limits(max_bytes=1000),
    )
    assert stopped.report.outcome == "exhausted" and stopped.context_json is None
    assert len(str(stopped.report.to_dict())) < 10_000


def test_required_cases_metadata_bounds_and_paid_failure_prefix_remain_truthful() -> (
    None
):
    module = _module("name", "urn:name")
    for invalid in (
        (),
        (replace(module, cases=()),),
        (module, module),
        (replace(module, cases=(replace(module.cases[0], expectations=()),)),),
    ):
        result = compose_contexts(invalid, resolution="retain-required")
        assert not result.report.verified and result.context_json is None
    metadata = compose_contexts(
        (replace(module, name="x" * 100_000),),
        resolution="retain-required",
        limits=Limits(max_bytes=1000),
    )
    assert metadata.report.outcome == "exhausted" and metadata.order == ()
    failing = replace(
        module,
        cases=(
            replace(
                module.cases[0],
                expectations=(ContextExpectation(("name",), "urn:expected"),),
            ),
        ),
    )
    budget = Budget()
    complete = compose_contexts(
        (failing, _module("extra", "urn:extra")),
        resolution="retain-required",
        budget=budget,
    )
    assert complete.report.outcome == "disagreement" and complete.context_json is None
    spent = budget.limits.max_work - budget.remaining("work")
    paid_exhaustions = 0
    for cap in range(max(1, spent - 1000), spent, 97):
        bounded = Budget(Limits(max_work=cap))
        result = compose_contexts(
            (failing, _module("extra", "urn:extra")),
            resolution="retain-required",
            budget=bounded,
        )
        paid = {id(issue) for issue in bounded._retained_issues.values()}
        assert paid <= _issues(result.report)
        assert result.context_json is None and not result.report.verified
        if paid and result.report.outcome == "exhausted":
            paid_exhaustions += 1
            assert result.cases[0].independent is not None
    assert paid_exhaustions
