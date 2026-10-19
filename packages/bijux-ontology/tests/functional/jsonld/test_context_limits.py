"""Real input, resource, work and retained-finding limits remain observable."""

import tracemalloc
from pathlib import Path

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.conversion import convert_to_rdf
from bijux_ontology.jsonld.processing.expansion import expand_document
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.resources import (
    ContextResources,
    OfflineDocumentLoader,
    context_document_loader,
    context_resources,
)
from bijux_ontology.jsonld.usages.checking import check_context, prepare_usages

EX = "https://example.org/"


@pytest.mark.parametrize(
    "limits",
    [
        Limits(max_bytes=16),
        Limits(max_depth=1),
        Limits(max_work=1),
        Limits(max_states=3),
    ],
)
def test_owned_context_limits_stop_before_success(limits: Limits) -> None:
    document = {"@context": {"p": EX + "p"}, "p": "value"}
    report = check_context(
        document, [ContextExpectation(("p",), EX + "p")], limits=limits
    )
    assert report.exit_code() == 2
    assert report.outcome == "exhausted"
    assert not report.verified


def test_later_exhaustion_retains_the_first_completed_disagreement() -> None:
    document = {"@context": {"@vocab": EX}, "p": "a", "q": "b"}
    report = check_context(
        document,
        [
            ContextExpectation(("p",), EX + "wrong"),
            ContextExpectation(("q",), EX + "wrong"),
        ],
        limits=Limits(max_issues=1),
    )
    assert report.exit_code() == 2
    assert not report.complete
    assert [issue.kind for issue in report.issues] == ["finding", "exhausted"]
    assert report.issues[0].location == "/p"


def test_resources_and_cumulative_bytes_use_the_supplied_operation_budget() -> None:
    loader = OfflineDocumentLoader(
        {
            EX + "first": {"@context": EX + "second"},
            EX + "second": {"@context": {"p": EX + "p"}},
        }
    )
    document = {"@context": EX + "first", "p": "text"}
    expected = [ContextExpectation(("p",), EX + "p")]
    for limits in (Limits(max_resources=1), Limits(max_total_bytes=60)):
        report = check_context(
            document, expected, document_loader=loader, budget=Budget(limits)
        )
        assert report.outcome == "exhausted"
    conflict = check_context(
        document, expected, limits=Limits(max_work=10), budget=Budget()
    )
    assert conflict.outcome == "input_error"


def test_rdf_output_limits_cannot_return_partial_conversion_as_success() -> None:
    result = convert_to_rdf(
        {EX + "p": "a", EX + "q": "b"}, limits=Limits(max_triples=1)
    )
    assert result.nquads == ""
    assert result.report.outcome == "exhausted"
    assert result.report.exit_code() == 2


def test_offline_context_files_reuse_duplicate_aware_json_decoding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "context.json"
    path.write_text(
        '{"@context":{"p":"https://example.org/p","p":"https://example.org/q"}}'
    )
    with pytest.raises(InputError, match="Duplicate"):
        OfflineDocumentLoader({EX + "context": path})(EX + "context", {})


@pytest.mark.parametrize("value", [10**309, -(10**309)])
def test_finite_json_numbers_outside_processor_range_are_execution_limits(
    value: int,
) -> None:
    document = {EX + "p": value}
    checked = check_context(document, [ContextExpectation((EX + "p",), EX + "p")])
    assert checked.outcome == "exhausted"
    assert all(issue.kind != "input_error" for issue in checked.issues)
    converted = convert_to_rdf(document)
    assert converted.report.outcome == "exhausted"
    assert converted.nquads == ""


def test_owned_resources_share_snapshots_without_rebinding_the_operation() -> None:
    calls: list[str] = []
    document: dict[str, object] = {"@context": {"p": EX + "p"}}

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Expose source and returned-response mutation across repeated reads."""
        calls.append(url)
        return {"documentUrl": EX + "effective", "document": document}

    budget = Budget(Limits(max_resources=1))
    resources = context_resources(loader, budget=budget)
    response = resources(EX + "context", {})
    response["document"] = None
    document["@context"] = {"p": EX + "changed"}
    assert resources(EX + "context", {})["document"] == {"@context": {"p": EX + "p"}}
    assert resources.effective_url(EX + "context") == EX + "effective"
    assert calls == [EX + "context"]
    assert context_resources(resources) is resources
    assert context_document_loader(resources, budget=budget) is resources
    with pytest.raises(AttributeError):
        resources.budget = Budget()  # type: ignore[misc]
    with pytest.raises(InputError, match="nesting"):
        ContextResources(resources)
    with pytest.raises(InputError, match="limits"):
        context_resources(resources, limits=Limits(max_resources=2))
    with pytest.raises(LimitExceeded, match="max_resources"):
        resources(EX + "another", {})
    assert calls == [EX + "context"]


def test_consumers_cannot_move_cached_resources_to_an_equal_but_distinct_budget() -> (
    None
):
    limits = Limits(max_resources=1)
    resources = context_resources(budget=Budget(limits))
    other = Budget(limits)
    document = {EX + "p": "value"}
    for acquire in (context_resources, context_document_loader):
        with pytest.raises(InputError, match="different operation budget"):
            acquire(resources, budget=other)
    for consumer in (expand_document, prepare_usages, convert_to_rdf, prepare_jsonld):
        with pytest.raises(InputError, match="different operation budget"):
            consumer(document, document_loader=resources, budget=other)
    report = check_context(
        document,
        [ContextExpectation((EX + "p",), EX + "p")],
        document_loader=resources,
        budget=other,
    )
    assert report.outcome == "input_error"
    assert not report.verified


@pytest.mark.parametrize("limits", [Limits(max_bytes=200), Limits(max_depth=1)])
def test_resource_forks_bound_whole_responses_without_poisoning_shared_capture(
    limits: Limits,
) -> None:
    calls: list[str] = []
    document: dict[str, object] = {"@context": {"p": EX + "p"}}

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Return tiny content with separately significant response metadata."""
        calls.append(url)
        return {
            "document": document,
            "documentUrl": EX + "effective",
            "contentType": "application/ld+json; profile=" + "x" * 1000,
        }

    resources = context_resources(loader)
    resources(EX + "context", {})
    document["@context"] = {"p": EX + "changed"}
    with pytest.raises(LimitExceeded):
        resources.for_budget(Budget(limits))(EX + "context", {})
    child = resources.for_budget(Budget())
    response = child(EX + "context", {})
    assert response["document"] == {"@context": {"p": EX + "p"}}
    response["document"] = None
    assert (
        child(EX + "context", {})["document"]
        == resources(EX + "context", {})["document"]
    )
    assert calls == [EX + "context"]


def test_resource_copies_prepaid_before_initial_and_cached_allocation() -> None:
    """Remote resource snapshots and public cache copies obey remaining bytes."""
    response: dict[str, object] = {
        "documentUrl": EX + "context",
        "contextUrl": None,
        "document": {"@context": {"p": EX + "x" * 500000}},
    }

    def load(url: str, options: dict[str, object]) -> dict[str, object]:
        """Supply an ordinary closed response without an executing processor."""
        return response

    resources = ContextResources(load, budget=Budget(Limits(max_total_bytes=32768)))
    tracemalloc.start()
    try:
        with pytest.raises(LimitExceeded):
            resources(EX + "context", {})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100000

    response["document"] = {"@context": [{f"p{i}": EX + f"p{i}"} for i in range(5000)]}
    budget = Budget()
    resources = ContextResources(load, budget=budget)
    original = resources(EX + "context", {})
    budget.consume("bytes", budget.remaining("bytes") - 128)
    tracemalloc.start()
    try:
        with pytest.raises(LimitExceeded):
            resources(EX + "context", {})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100000
    # A deliberately independent child budget owns its copies of the same cache.
    fork = resources.for_budget(Budget())
    owned = fork(EX + "context", {})
    assert owned == original and owned is not original


def test_offline_file_capture_uses_the_enclosing_resource_allowance(
    tmp_path: Path,
) -> None:
    """An owned offline loader cannot allocate a full file before shared bounds."""
    path = tmp_path / "context.json"
    path.write_text('{"@context":{"p":"' + EX + "x" * 500000 + '"}}')
    loader = OfflineDocumentLoader({EX + "context": path})
    resources = ContextResources(loader, budget=Budget(Limits(max_total_bytes=32768)))
    tracemalloc.start()
    try:
        with pytest.raises(LimitExceeded):
            resources(EX + "context", {})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 100000
