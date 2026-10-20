"""Shared preparation preserves original source, graph and blank identities."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.paths import SourcePath
from bijux_ontology.jsonld import checking
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.expansion import expand_document

EX = "https://example.org/"


def _records(
    size: int, *, keyed: bool = False
) -> tuple[dict[str, object], list[SourcePath]]:
    records = [
        {"@id": f"record/{i}", "@type": "Record", "name": ["same", "same"], "other": i}
        for i in range(size)
    ]
    return (
        {
            "@context": {"@vocab": EX},
            "records": {f"key{i}": record for i, record in enumerate(records)}
            if keyed
            else records,
        },
        [("records", f"key{i}" if keyed else i, "name") for i in range(size)],
    )


@pytest.mark.parametrize("keyed", [False, True])
def test_batch_matches_original_observations_and_caches_growing_corpus(
    keyed: bool,
) -> None:
    document, paths = _records(30, keyed=keyed)
    before = deepcopy(document)
    original = prepare_usages(document, base=EX)
    expected = tuple(original.observe(path) for path in paths)
    with patch.object(checking, "expand_document", wraps=expand_document) as expand:
        prepared = prepare_usages(document, base=EX)
        actual = prepared.observe_many(paths)
        assert expand.call_count == 4
        assert prepared.observe_many([paths[-1], *paths, paths[0]]) == (
            actual[-1],
            *actual,
            actual[0],
        )
        assert expand.call_count == 4
    assert actual == expected
    assert all(len(item.occurrences) == 2 for item in actual)
    assert document == before


@pytest.mark.parametrize("graph", [EX + "graph", "_:graph"])
def test_original_graph_and_shared_blank_object_tokens_survive_projection(
    graph: str,
) -> None:
    document: dict[str, object] = {
        "@context": {"@vocab": EX, "link": {"@id": EX + "link", "@type": "@id"}},
        "@id": graph,
        "@graph": [
            {"@id": "left", "link": "_:shared", "name": "left"},
            {"@id": "right", "link": "_:shared", "name": "right"},
            {"@id": "third", "link": "_:different", "name": "third"},
        ],
    }
    paths: list[SourcePath] = [("@graph", i, "link") for i in range(3)]
    baseline = prepare_usages(document, base=EX)
    expected = tuple(baseline.observe(path) for path in paths)
    actual = prepare_usages(document, base=EX).observe_many(paths)
    assert actual == expected
    assert not any(usage.limitation for usage in actual)
    tokens = [usage.occurrences[0].traits.identifier for usage in actual]
    assert tokens[0] == tokens[1] != tokens[2]


@pytest.mark.parametrize("split_graphs", [False, True])
def test_repeated_subject_descriptions_retain_conservative_fallback(
    split_graphs: bool,
) -> None:
    nodes: list[object] = [
        {"@id": EX + "same", "name": "first"},
        {"@id": EX + "same", "name": "second"},
    ]
    if split_graphs:
        nodes = [
            {"@id": EX + f"graph/{i}", "@graph": [node]} for i, node in enumerate(nodes)
        ]
    document: dict[str, object] = {"@context": {"@vocab": EX}, "@graph": nodes}
    paths: list[SourcePath] = [
        ("@graph", i, "@graph", 0, "name") if split_graphs else ("@graph", i, "name")
        for i in range(2)
    ]
    baseline = prepare_usages(document)
    expected = tuple(baseline.observe(path) for path in paths)
    assert prepare_usages(document).observe_many(paths) == expected


@pytest.mark.parametrize(
    "kind", ["blank", "scoped", "type_scope", "list", "opaque", "alias"]
)
def test_unsupported_cohorts_keep_maintained_differential_behavior(kind: str) -> None:
    context: dict[str, object] = {"@vocab": EX}
    nodes: list[dict[str, object]] = [
        {"@id": EX + str(i), "name": ["left", "right"], "@type": "Record"}
        for i in range(2)
    ]
    if kind == "blank":
        for i, node in enumerate(nodes):
            node["@id"] = f"_:record{i}"
    elif kind == "scoped":
        context["records"] = {"@id": EX + "records", "@context": {"name": EX + "label"}}
    elif kind == "type_scope":
        context["Record"] = {"@id": EX + "Record", "@context": {"name": EX + "label"}}
    elif kind == "list":
        context["name"] = {"@id": EX + "name", "@container": "@list"}
    elif kind == "opaque":
        context["name"] = {"@id": EX + "name", "@type": "@json"}
    else:
        context["identity"] = "@id"
        for node in nodes:
            node["identity"] = node.pop("@id")
    document: dict[str, object] = {"@context": context, "records": nodes}
    paths: list[SourcePath] = [("records", i, "name") for i in range(2)]
    baseline = prepare_usages(document)
    expected = tuple(baseline.observe(path) for path in paths)
    assert prepare_usages(document).observe_many(paths) == expected


def test_batch_report_matches_sequential_and_exhaustion_retains_existing_findings() -> (
    None
):
    document, paths = _records(4)
    expectations = [ContextExpectation(path, EX + "wrong") for path in paths]
    baseline = prepare_usages(document, base=EX)
    for path in paths:
        baseline.observe(path)
    expected = baseline.check(expectations)
    assert prepare_usages(document, base=EX).check(expectations) == expected
    document["@context"] = {"@vocab": EX, "dropped": None}
    document["dropped"] = "retain this finding"
    budget = Budget(Limits(max_work=100_000))
    prepared = prepare_usages(document, base=EX, budget=budget)
    budget.consume("work", budget.limits.max_work - budget._counts.get("work", 0) - 100)
    exhausted = prepared.check(expectations)
    assert not exhausted.complete and not exhausted.verified
    assert {issue.code for issue in exhausted.issues} >= {
        "context.property_dropped",
        "execution.limit_exceeded",
    }


def test_batch_missing_path_and_invalid_selector_keep_precise_errors() -> None:
    document, paths = _records(2)
    prepared = prepare_usages(document, base=EX)
    with pytest.raises(InputError, match="No supplied usage") as missing:
        prepared.observe_many([*paths, ("records", 5, "name")])
    assert missing.value.location == "/records/5/name"
    with pytest.raises(InputError, match="key/index"):
        prepared.observe_many([("records", True, "name")])
    with pytest.raises(LimitExceeded, match="max_states"):
        prepare_usages(document, base=EX, limits=Limits(max_states=1))
    budget = Budget()
    bounded = prepare_usages(document, base=EX, budget=budget)
    budget.consume("states", budget.remaining("states"))
    with pytest.raises(LimitExceeded, match="max_states"):
        bounded.observe_many(paths)


def test_named_parent_and_child_cohort_does_not_claim_ancestor_edges() -> None:
    document: dict[str, object] = {
        "@context": {"@vocab": EX},
        "@id": EX + "parent",
        "name": "Parent",
        "child": {"@id": EX + "child", "name": "Child"},
    }
    paths: list[SourcePath] = [("name",), ("child", "name")]
    baseline = prepare_usages(document)
    expected = tuple(baseline.observe(path) for path in paths)
    actual = prepare_usages(document).observe_many(paths)
    assert actual == expected
    assert all(
        [item.predicate for item in usage.occurrences] == [EX + "name"]
        for usage in actual
    )
