"""Actual processed definitions differ independently of current sample records."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import overload

import pytest

from bijux_ontology.absence import ABSENT
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.contexts import (
    ContextScope,
    compare_context_definitions,
    inspect_context,
)
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader

EX = "https://example.org/v#"
PREFIX = {"@id": EX, "@prefix": True}


def test_effective_reordering_compact_iris_and_unused_coercions() -> None:
    """Unused definitions are inspected without fabricating a sample document."""
    first = {
        "ex": PREFIX,
        "label": "ex:label",
        "unused": {"@id": "ex:unused", "@type": "@id"},
    }
    same = {
        "unused": {"@type": "@id", "@id": EX + "unused"},
        "label": EX + "label",
        "ex": PREFIX,
    }
    result = compare_context_definitions(first, same)
    assert result.report.verified, result.report.to_dict()
    assert result.changes == ()
    selected = compare_context_definitions(
        first, {"label": EX + "label"}, terms=("label",)
    )
    assert selected.report.verified, selected.report.to_dict()
    changed = deepcopy(same)
    changed["unused"] = {"@id": EX + "other", "@type": "@vocab"}
    compared = compare_context_definitions(first, changed)
    assert compared.report.outcome == "disagreement"
    assert {(item.term, item.facet) for item in compared.changes} == {
        ("unused", "iri"),
        ("unused", "type_mapping"),
    }


def test_protection_defaults_nullification_and_illegal_candidates() -> None:
    """Removed protection/defaults are visible while real illegal overrides fail."""
    first = {
        "@vocab": EX,
        "label": {"@id": EX + "label", "@protected": True},
        "ignored": None,
    }
    second = {"@vocab": "https://example.org/other#", "label": EX + "label"}
    result = compare_context_definitions(first, second)
    assert {(item.term, item.facet) for item in result.changes} >= {
        (None, "vocabulary"),
        ("label", "protected"),
        ("ignored", "definition"),
    }
    view = inspect_context(first).view
    assert view is not None
    assert next(item for item in view.terms if item.term == "ignored").iri is None
    for local, code in (
        (None, "invalid context nullification"),
        ({"label": EX + "changed"}, "protected term redefinition"),
    ):
        invalid = inspect_context(local, inherited_context=first)
        assert invalid.view is None and invalid.report.outcome == "input_error"
        assert any(issue.actual == code for issue in invalid.report.issues)
    empty_contexts: tuple[object, ...] = ([], None, {})
    for mode in ("json-ld-1.0", "json-ld-1.1"):
        for empty in empty_contexts:
            initial = inspect_context(empty, processing_mode=mode)
            assert initial.report.verified and initial.view is not None
            assert (
                initial.view.defaults.processing_mode == mode
                and initial.view.terms == ()
            )
    removed = compare_context_definitions({"@vocab": EX, "unused": EX + "unused"}, None)
    assert removed.report.outcome == "disagreement"
    assert {(item.term, item.facet) for item in removed.changes} >= {
        (None, "vocabulary"),
        ("unused", "definition"),
    }
    base_equal = compare_context_definitions(
        {"@base": "../"},
        {"@base": "https://example.org/"},
        before_base="https://example.org/path/doc",
        after_base="https://example.org/path/doc",
    )
    assert base_equal.report.verified, base_equal.report.to_dict()
    bad_mode = inspect_context({"@direction": "rtl"}, processing_mode="json-ld-1.0")
    assert bad_mode.report.outcome == "input_error" and bad_mode.view is None


def test_processed_flags_containers_index_coercion_and_literal_overrides() -> None:
    """Index property identity and index-value coercion are separate semantics."""
    context = {
        "ex": PREFIX,
        "@language": "EN",
        "@direction": "rtl",
        "plain": "ex:plain",
        "clear": {"@id": "ex:clear", "@language": None, "@direction": None},
        "by": {"@id": "ex:key", "@type": "@id"},
        "indexed": {
            "@id": "ex:items",
            "@container": ["@index", "@set"],
            "@index": "by",
        },
        "reverse": {"@reverse": "ex:parent"},
        "listed": {"@id": "ex:listed", "@container": "@list", "@type": "@id"},
    }
    result = inspect_context(context, base="https://example.org/document")
    assert result.report.verified, result.report.to_dict()
    assert result.view is not None
    terms = {item.term: item for item in result.view.terms}
    assert terms["ex"].prefix and terms["reverse"].reverse
    assert (
        terms["listed"].containers == ("@list",)
        and terms["listed"].type_mapping == "@id"
    )
    assert (
        terms["plain"].language is ABSENT and terms["plain"].effective_language == "en"
    )
    assert terms["clear"].language is None and terms["clear"].effective_language is None
    assert (
        terms["clear"].direction is None and terms["clear"].effective_direction is None
    )
    assert terms["indexed"].index == EX + "key" and terms["indexed"].index_type == "@id"
    reordered = deepcopy(context)
    reordered["indexed"] = {
        "@id": EX + "items",
        "@container": ["@set", "@index"],
        "@index": "by",
    }
    assert compare_context_definitions(
        context,
        reordered,
        before_base="https://example.org/document",
        after_base="https://example.org/document",
    ).report.verified
    changed = deepcopy(context)
    changed["by"] = {"@id": "ex:key", "@type": "@vocab"}
    difference = compare_context_definitions(
        context,
        changed,
        terms=("indexed",),
        before_base="https://example.org/document",
        after_base="https://example.org/document",
    )
    assert any(
        item.term == "indexed" and item.facet == "index_type"
        for item in difference.changes
    )
    nesting = {"nest": "@nest", "label": {"@id": EX + "label", "@nest": "nest"}}
    canonical = {"nest": "@nest", "label": {"@id": EX + "label", "@nest": "@nest"}}
    assert compare_context_definitions(nesting, canonical).report.verified


def test_selected_scopes_require_parent_and_keep_type_reversion() -> None:
    """Property and type transitions use actual distinct processor semantics."""
    context = {"Thing": {"@id": "ex:Thing", "@context": {"label": "ex:label"}}}
    selected = (ContextScope("Thing", "type"),)
    unresolved = inspect_context(context, scopes=selected)
    assert unresolved.report.outcome == "unverified" and unresolved.scopes[0][1] is None
    parent = {"ex": PREFIX}
    result = inspect_context(context, inherited_context=parent, scopes=selected)
    assert result.report.verified, result.report.to_dict()
    scoped = result.scopes[0][1]
    assert scoped is not None and scoped.previous is not None
    assert (
        next(item for item in scoped.terms if item.term == "label").iri == EX + "label"
    )
    changed = {"Thing": {"@id": "ex:Thing", "@context": {"label": "ex:changed"}}}
    compared = compare_context_definitions(
        context,
        changed,
        scopes=selected,
        before_inherited=parent,
        after_inherited=parent,
    )
    assert any(
        item.scope == "scopes/0" and item.term == "label" and item.facet == "iri"
        for item in compared.changes
    )
    assert not compare_context_definitions(context, context).report.verified
    nested_before = {
        "outer": {
            "@id": EX + "outer",
            "@context": {
                "inner": {"@id": EX + "inner", "@context": {"label": EX + "old"}}
            },
        }
    }
    nested_after = {
        "outer": {
            "@id": EX + "outer",
            "@context": {
                "inner": {"@id": EX + "inner", "@context": {"label": EX + "new"}}
            },
        }
    }
    for candidate in (nested_before, nested_after):
        nested = compare_context_definitions(
            nested_before,
            candidate,
            scopes=(ContextScope("outer"),),
            before_inherited=None,
            after_inherited=None,
        )
        assert not nested.report.verified and not nested.report.complete
        assert any(
            issue.code == "context.definition_unverified" and "inner" in issue.location
            for issue in nested.report.issues
        )
    protected = {
        "label": {"@id": EX + "label", "@protected": True},
        "Thing": {"@id": EX + "Thing", "@context": {"label": EX + "other"}},
    }
    property_result = inspect_context(
        protected, inherited_context=None, scopes=(ContextScope("Thing", "property"),)
    )
    assert property_result.report.verified, property_result.report.to_dict()
    assert property_result.scopes[0][1] is not None
    assert property_result.scopes[0][1].previous is None
    forbidden = inspect_context(protected, inherited_context=None, scopes=selected)
    assert forbidden.view is None and forbidden.report.outcome == "input_error"


def test_offline_import_effective_url_and_independent_versions(tmp_path: Path) -> None:
    """Resource snapshots retain effective base and never mutate caller documents."""
    request = "https://example.org/context"
    effective = "https://cdn.example/contexts/root.json"
    common = "https://cdn.example/contexts/common.json"
    documents = {
        request: {"@context": {"@import": "common.json", "label": "ex:label"}},
        common: {"@context": {"ex": PREFIX}},
    }
    original = deepcopy(documents)
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        return {
            "documentUrl": effective if url == request else url,
            "contextUrl": None,
            "document": documents[url],
        }

    inspected = inspect_context(request, document_loader=loader)
    assert inspected.report.verified, inspected.report.to_dict()
    assert calls == [request, common] and documents == original
    assert inspected.view is not None
    assert (
        next(item for item in inspected.view.terms if item.term == "label").iri
        == EX + "label"
    )
    before = OfflineDocumentLoader({request: {"@context": {"unused": EX + "old"}}})
    after = OfflineDocumentLoader({request: {"@context": {"unused": EX + "new"}}})
    difference = compare_context_definitions(
        request, request, before_loader=before, after_loader=after
    )
    assert any(
        item.term == "unused" and item.facet == "iri" for item in difference.changes
    )
    assert inspect_context(request).report.outcome == "input_error"
    malformed = tmp_path / "context.json"
    malformed.write_text('{"@context":\n]')
    rejected = inspect_context(
        request, document_loader=OfflineDocumentLoader({request: malformed})
    )
    assert rejected.report.outcome == "input_error"
    details = rejected.report.issues[0].details
    assert details is not None and details.failure is not None
    assert details.failure.document_iri == request
    assert details.failure.location == str(malformed)
    assert details.failure.span is not None
    assert (details.failure.span.start_line, details.failure.span.start_column) == (
        2,
        1,
    )
    actual_after = {"unused": EX + "after"}

    def mutating_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        actual_after["unused"] = EX + "before"
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {"@context": {"unused": EX + "before"}},
        }

    retained = compare_context_definitions(
        request, actual_after, before_loader=mutating_loader
    )
    assert retained.report.outcome == "disagreement"
    assert retained.changes[0].after == EX + "after"


def test_owned_records_indexed_selections_and_cumulative_bounds() -> None:
    """Tiny limits stop before hashing/formatting and retain prior definite changes."""
    context = {"label": EX + "label", "other": EX + "other"}
    inspected = inspect_context(context)
    context["label"] = EX + "changed"
    assert inspected.view is not None and inspected.view.terms[0].iri == EX + "label"
    data = inspected.data
    assert isinstance(data, dict)
    data.clear()
    assert inspected.data != {}
    scope = ContextScope("Thing")

    class Indexed(Sequence[ContextScope]):
        def __len__(self) -> int:
            return 1

        @overload
        def __getitem__(self, index: int) -> ContextScope: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[ContextScope]: ...

        def __getitem__(
            self, index: int | slice
        ) -> ContextScope | Sequence[ContextScope]:
            assert type(index) is int and index == 0
            return scope

        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("Unbounded iteration must never run")

    scoped = inspect_context(
        {"Thing": {"@id": EX + "Thing", "@context": {}}},
        inherited_context=None,
        scopes=Indexed(),
    )
    assert scoped.report.verified
    giant = replace(scope, term="a" * 100000)
    exhausted = inspect_context({}, scopes=(giant,), limits=Limits(max_bytes=1000))
    assert exhausted.report.outcome == "exhausted" and exhausted.view is None
    assert all(len(issue.location) < 1000 for issue in exhausted.report.issues)
    assert (
        inspect_context(
            {"a" * 100000: EX + "p"}, limits=Limits(max_bytes=1000)
        ).report.outcome
        == "exhausted"
    )
    budget = Budget(Limits(max_work=1))
    assert inspect_context({}, budget=budget).report.outcome == "exhausted"
    capped = compare_context_definitions(
        {"a": EX + "a", "b": EX + "b"},
        {"a": EX + "c", "b": EX + "d"},
        limits=Limits(max_issues=1),
    )
    assert capped.report.outcome == "exhausted"
    assert any(issue.kind == "finding" for issue in capped.report.issues)
    assert any(issue.kind == "exhausted" for issue in capped.report.issues)
    for work in range(1, 1500, 41):
        bounded = inspect_context({"label": EX + "label"}, limits=Limits(max_work=work))
        assert bounded.report.outcome in ("exhausted", "agreement")
        if bounded.report.outcome == "exhausted":
            assert bounded.view is None
    with pytest.raises(RuntimeError, match="caller"):
        inspect_context(
            "https://example.org/unavailable",
            document_loader=lambda *_: (_ for _ in ()).throw(RuntimeError("caller")),
        )


def test_existing_worker_matches_inspection_scopes_and_resource_versions(
    tmp_path: Path,
) -> None:
    """The installed interpreter executes these exact bounded public operations."""
    import sys

    from bijux_ontology.interfaces.execution.executor import WorkerExecutor

    worker = WorkerExecutor(python=sys.executable, artifacts=tmp_path)
    context = {"label": {"@id": EX + "label", "@protected": True}}
    direct = inspect_context(context)
    isolated = worker.run("context_inspection", {"context": context})
    assert isolated.report.to_dict() == direct.report.to_dict()
    assert isolated.data == direct.data
    scoped = {"Thing": {"@id": EX + "Thing", "@context": {"label": EX + "label"}}}
    missing = worker.run(
        "context_inspection",
        {"context": scoped, "scopes": [{"term": "Thing", "kind": "type"}]},
    )
    assert missing.report.outcome == "unverified"
    initial = worker.run(
        "context_inspection",
        {
            "context": scoped,
            "scopes": [{"term": "Thing", "kind": "type"}],
            "inherited_context": None,
        },
    )
    assert initial.report.verified
    illegal = worker.run(
        "context_inspection", {"context": None, "inherited_context": context}
    )
    assert illegal.report.outcome == "input_error"
    assert isinstance(illegal.data, dict) and illegal.data["view"] is None
    url = "https://example.org/context"
    inputs: dict[str, object] = {
        "before": url,
        "after": url,
        "before_resources": {url: {"@context": {"unused": EX + "a"}}},
        "after_resources": {url: {"@context": {"unused": EX + "b"}}},
    }
    compared = worker.run("context_definitions", inputs)
    assert compared.report.outcome == "disagreement"
    assert isinstance(compared.data, dict) and compared.data["changes"]
