"""A context corpus distinguishes scoped regressions, baseline defects and coverage."""

import pytest

from bijux_ontology.absence import ABSENT
from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.workflows.upgrades.corpus import (
    ContextCorpusDocument,
    CorpusBranch,
    CorpusWitness,
    assess_context_corpus,
)

EX = "https://example.org/"
CONTEXT = EX + "context"


def loader(context: object) -> OfflineDocumentLoader:
    """Keep context resources explicit and offline in every corpus operation."""
    return OfflineDocumentLoader({CONTEXT: {"@context": context}})


def documents() -> tuple[ContextCorpusDocument, ...]:
    """Exercise independent serializer branches with public expected mappings."""
    return (
        ContextCorpusDocument(
            "labelled",
            {"@context": CONTEXT, "label": "Record"},
            (ContextExpectation(("label",), EX + "label", value_kind="literal"),),
        ),
        ContextCorpusDocument(
            "linked",
            {"@context": CONTEXT, "@id": EX + "record", "friend": EX + "other"},
            (ContextExpectation(("friend",), EX + "friend", value_kind="iri"),),
        ),
    )


def context() -> dict[str, object]:
    """Name one stable field and one context-sensitive link field."""
    return {"label": EX + "label", "friend": {"@id": EX + "friend", "@type": "@id"}}


def test_unchanged_corpus_verifies_only_its_explicit_exercised_coverage() -> None:
    branch = CorpusBranch("relationship", (CorpusWitness("linked", ("friend",)),))
    result = assess_context_corpus(
        documents(),
        before_loader=loader(context()),
        after_loader=loader(context()),
        branches=(branch,),
    )
    assert result.compatible is True and result.report.verified
    assert result.candidate_report.verified
    assert all(
        item.before.verified
        and item.after.verified
        and item.usages.verified
        and item.dataset.verified
        for item in result.documents
    )
    assert result.coverage[0].report.verified
    (evidence,) = result.coverage[0].evidence
    assert (
        evidence.exercised
        and evidence.before is not None
        and evidence.after is not None
    )
    assert evidence.before.occurrences[0].predicate == EX + "friend"
    assert not any(item.changes for item in result.documents)


@pytest.mark.parametrize("change", ["reverse", "coercion"])
def test_only_affected_document_and_field_are_identified(change: str) -> None:
    new = context()
    new["friend"] = (
        {"@reverse": EX + "friend", "@type": "@id"}
        if change == "reverse"
        else EX + "friend"
    )
    result = assess_context_corpus(
        documents(), before_loader=loader(context()), after_loader=loader(new)
    )
    stable, changed = result.documents
    assert stable.report.verified
    assert changed.before.verified and not changed.after.verified
    assert changed.demonstrated_break and result.compatible is False
    assert any(
        item.status == "introduced"
        and item.after is not None
        and item.after.location == "/friend"
        for item in changed.changes
    )
    assert any(
        issue.code == "context.usage_meaning_changed" and issue.location == "/friend"
        for issue in changed.usages.issues
    )
    assert any(
        "/documents/linked/" in issue.location and issue.location.endswith("/friend")
        for issue in result.report.issues
    )
    assert all(
        "/documents/labelled/" not in issue.location for issue in result.report.issues
    )


def test_exact_dataset_comparison_detects_topology_outside_selected_leaf() -> None:
    mapping = {
        "label": EX + "label",
        "first": {"@id": EX + "first", "@type": "@id"},
        "second": {"@id": EX + "second", "@type": "@id"},
        "name": EX + "name",
    }
    old = {
        "@context": CONTEXT,
        "@graph": [
            {"@id": EX + "root", "label": "Root", "first": "_:a", "second": "_:a"},
            {"@id": "_:a", "name": "same"},
            {"@id": "_:b", "name": "same"},
        ],
    }
    new = {
        "@context": CONTEXT,
        "@graph": [
            {"@id": EX + "root", "label": "Root", "first": "_:a", "second": "_:b"},
            {"@id": "_:a", "name": "same"},
            {"@id": "_:b", "name": "same"},
        ],
    }
    member = ContextCorpusDocument(
        "shared-nodes",
        old,
        (ContextExpectation(("@graph", 0, "label"), EX + "label"),),
        after=new,
    )
    result = assess_context_corpus(
        (member,), before_loader=loader(mapping), after_loader=loader(mapping)
    )
    (checked,) = result.documents
    assert (
        checked.before.verified and checked.after.verified and checked.usages.verified
    )
    assert checked.dataset.outcome == "disagreement"
    assert result.compatible is False


def test_baseline_failures_resolutions_and_candidate_truth_remain_separate() -> None:
    baseline = context()
    baseline["label"] = EX + "wrong"
    (member,) = documents()[:1]
    persistent = assess_context_corpus(
        (member,), before_loader=loader(baseline), after_loader=loader(baseline)
    )
    assert persistent.compatible is None and not persistent.report.verified
    assert persistent.documents[0].changes[0].status == "persistent"
    assert (
        persistent.documents[0].before.issues and persistent.documents[0].after.issues
    )
    resolved = assess_context_corpus(
        (member,), before_loader=loader(baseline), after_loader=loader(context())
    )
    assert resolved.candidate_report.verified
    assert resolved.compatible is None and not resolved.report.verified
    assert any(item.status == "resolved" for item in resolved.documents[0].changes)
    assert resolved.documents[0].before.issues


def test_missing_baseline_and_json_null_cannot_prove_compatibility() -> None:
    current = documents()[0]
    missing = ContextCorpusDocument(
        "missing", ABSENT, current.expectations, after=current.before
    )
    result = assess_context_corpus(
        (missing,), before_loader=loader(context()), after_loader=loader(context())
    )
    assert result.candidate_report.verified and result.compatible is None
    assert any(
        issue.code == "upgrade.corpus_baseline_missing"
        for issue in result.documents[0].before.issues
    )
    null = ContextCorpusDocument("null", None, current.expectations)
    result = assess_context_corpus(
        (null,), before_loader=loader(context()), after_loader=loader(context())
    )
    assert result.compatible is None
    assert all(
        issue.code != "upgrade.corpus_baseline_missing"
        for issue in result.report.issues
    )


def test_unexercised_branch_and_repeated_witness_cannot_manufacture_coverage() -> None:
    member = documents()[0]
    missing = CorpusBranch("optional-link", (CorpusWitness("labelled", ("friend",)),))
    duplicate = CorpusWitness("labelled", ("label",))
    repeated = CorpusBranch("two-examples", (duplicate, duplicate), minimum=2)
    result = assess_context_corpus(
        (member,),
        before_loader=loader(context()),
        after_loader=loader(context()),
        branches=(missing, repeated),
    )
    assert result.documents[0].before.verified and result.candidate_report.verified
    assert result.compatible is None and not result.report.verified
    assert [item.report.verified for item in result.coverage] == [False, False]
    assert result.coverage[0].evidence[0].before is None
    assert len(result.coverage[1].evidence) == 1


def test_reused_loader_has_independent_version_snapshots_across_documents() -> None:
    old, new = context(), context()
    new["friend"] = EX + "friend"
    versions = iter((old, new))

    def changing(url: str, options: dict[str, object]) -> dict[str, object]:
        """Model one reused retrieval callable changing between release snapshots."""
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {"@context": next(versions)},
        }

    result = assess_context_corpus(
        documents(), before_loader=changing, after_loader=changing
    )
    assert result.documents[0].report.verified
    assert result.documents[1].before.verified
    assert result.documents[1].demonstrated_break


def test_exhausted_corpus_retains_completed_evidence_and_cannot_verify() -> None:
    result = assess_context_corpus(
        documents(),
        before_loader=loader(context()),
        after_loader=loader(context()),
        limits=Limits(max_work=1),
    )
    assert result.compatible is None and not result.report.verified
    assert len(result.documents) == 2
    assert any(issue.kind == "exhausted" for issue in result.report.issues)


def test_nested_raw_findings_are_retained_once_with_distinct_version_locations() -> (
    None
):
    mapping = context()
    mapping["label"] = EX + "wrong"
    member = documents()[0]
    result = assess_context_corpus(
        (member,), before_loader=loader(mapping), after_loader=loader(mapping)
    )
    (document,) = result.documents
    assert len(document.before.children) == len(document.after.children) == 2
    assert len(document.before.issues) == len(document.after.issues) == 1
    assert document.usages.verified and document.dataset.verified
    assert len(document.report.issues) == len(result.report.issues) == 2
    assert {issue.location for issue in result.report.issues} == {
        "/documents/labelled/baseline/label",
        "/documents/labelled/candidate/label",
    }
