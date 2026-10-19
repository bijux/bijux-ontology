"""Actual RFC source edits need explicit base, retained meaning and source form."""

import json
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.export import write_export
from bijux_ontology.documents.forms.types import FormRequirement
from bijux_ontology.documents.patches import (
    DocumentPatch,
    RetainedUsage,
    apply_document_patch,
    export_document_patch,
)
from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.selectors.types import ArrayItems, Property, SourceSelector
from bijux_ontology.rdf.comparison import compare_rdf

EX = "https://example.org/patch/"


def _document() -> dict[str, Any]:
    return {
        "@context": {"name": EX + "name", "code": EX + "code"},
        "@graph": [
            {"@id": EX + "one", "name": "Old", "code": "stable"},
            {"@id": EX + "two", "name": "Second", "code": "other"},
        ],
    }


def _forms() -> tuple[FormRequirement, ...]:
    return (
        FormRequirement(SourceSelector(()), shape="object", required_keys=("@graph",)),
        FormRequirement(SourceSelector((Property("@graph"),)), shape="array"),
        FormRequirement(
            SourceSelector((Property("@graph"), ArrayItems())),
            shape="object",
            required_keys=("@id", "name", "code"),
        ),
    )


def _apply(document: object, edits: Sequence[object], **options: Any) -> DocumentPatch:
    kwargs: dict[str, Any] = {
        "expected_base": deepcopy(document),
        "before_requirements": _forms(),
        "after_requirements": _forms(),
        "before_expectations": (
            ContextExpectation(("@graph", 0, "name"), EX + "name"),
        ),
        "after_expectations": (ContextExpectation(("@graph", 0, "name"), EX + "name"),),
        "retained": (RetainedUsage(("@graph", 0, "code"), ("@graph", 0, "code")),),
        "base": EX + "document",
    }
    kwargs.update(options)
    return apply_document_patch(document, edits, **kwargs)


def _backend() -> None:
    pytest.importorskip("jsonpatch")
    pytest.importorskip("pyld")


def test_field_context_edits_are_owned_and_export_exact_checked_bytes(
    tmp_path: Path,
) -> None:
    _backend()
    original = _document()
    unchanged = deepcopy(original)
    result = _apply(
        original,
        [
            {"op": "test", "path": "/@graph/0/@id", "value": EX + "one"},
            {"op": "replace", "path": "/@context/name", "value": EX + "title"},
            {"op": "replace", "path": "/@graph/0/name", "value": 'New "title"'},
        ],
        after_expectations=(ContextExpectation(("@graph", 0, "name"), EX + "title"),),
    )
    assert result.report.verified and result.operations_checked == 3
    assert original == unchanged
    assert result.before_form is not None and result.after_form is not None
    assert (
        result.before_form.source is not None and result.after_form.source is not None
    )
    expected = deepcopy(original)
    expected["@context"]["name"] = EX + "title"
    expected["@graph"][0]["name"] = 'New "title"'
    assert result.document == expected
    read = result.document
    assert isinstance(read, dict)
    read.clear()
    assert result.document == expected
    with pytest.raises(InputError):
        replace(result, document_json="{}")
    with pytest.raises(InputError):
        DocumentPatch()
    exported = export_document_patch(result)
    destination = tmp_path / "checked.jsonld"
    write_export(exported, destination)
    assert json.loads(destination.read_text()) == expected
    actual = prepare_jsonld(json.loads(destination.read_text()), base=EX + "document")
    assert actual.source is not None
    assert compare_rdf(result.after_form.source, actual.source).verified


def test_stale_base_failed_test_and_retained_mapping_preserve_inputs_and_destination(
    tmp_path: Path,
) -> None:
    _backend()
    original = _document()
    edits = [{"op": "replace", "path": "/@graph/0/name", "value": "Edited"}]
    shifted = deepcopy(original)
    shifted["@graph"].insert(
        0, {"@id": EX + "inserted", "name": "Inserted", "code": "stable"}
    )
    before = deepcopy(shifted)
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        raise AssertionError("Failed preconditions must precede loaders")

    stale = _apply(shifted, edits, expected_base=original, document_loader=loader)
    assert any(
        i.code == "documents.patch_precondition_failed" for i in stale.report.issues
    )
    assert shifted == before and not calls and stale.document is None
    assert stale.report.outcome == "disagreement"
    assert stale.report.checked == 1 and not stale.report.complete
    assert stale.before_form is None and stale.after_form is None
    assert not any(i.code == "input.empty_scope" for i in stale.report.issues)
    failed = _apply(
        original,
        [{"op": "test", "path": "/@graph/0/@id", "value": EX + "wrong"}, *edits],
    )
    assert any(
        i.code == "documents.patch_precondition_failed" for i in failed.report.issues
    )
    assert failed.report.outcome == "disagreement"
    assert failed.report.checked == 1 and not failed.report.complete
    assert not any(i.code == "input.empty_scope" for i in failed.report.issues)
    wrong = _apply(
        original,
        [
            {"op": "replace", "path": "/@context/code", "value": EX + "unintended"},
            *edits,
        ],
    )
    assert not wrong.report.verified and wrong.retained_report is not None
    assert any(i.kind == "finding" for i in wrong.retained_report.issues)
    destination = tmp_path / "existing.jsonld"
    destination.write_text("untouched")
    with pytest.raises(InputError):
        write_export(export_document_patch(wrong), destination, overwrite=True)
    assert destination.read_text() == "untouched"
    assert original == _document()


def test_array_rebinding_requires_actual_named_parent_and_exact_retained_value() -> (
    None
):
    _backend()
    document = _document()
    new = {"@id": EX + "inserted", "name": "Inserted", "code": "stable"}
    edits = [
        {"op": "add", "path": "/@graph/0", "value": new},
        {"op": "replace", "path": "/@graph/1/name", "value": "Edited"},
    ]
    good = _apply(
        document,
        edits,
        retained=(RetainedUsage(("@graph", 0, "code"), ("@graph", 1, "code")),),
        after_expectations=(ContextExpectation(("@graph", 1, "name"), EX + "name"),),
    )
    assert good.report.verified
    guessed = _apply(document, edits)
    assert not guessed.report.verified
    assert any(
        i.code == "documents.patch_rebinding_unverified" for i in guessed.report.issues
    )
    blank = _document()
    del blank["@graph"][0]["@id"]
    relaxed = (FormRequirement(SourceSelector(()), shape="object"),)
    ambiguous = _apply(
        blank,
        edits,
        before_requirements=relaxed,
        after_requirements=relaxed,
        retained=(RetainedUsage(("@graph", 0, "code"), ("@graph", 1, "code")),),
        after_expectations=(ContextExpectation(("@graph", 1, "name"), EX + "name"),),
    )
    assert not ambiguous.report.verified and ambiguous.document is None
    changed = _apply(
        document,
        [
            {"op": "replace", "path": "/@graph/0/code", "value": "different"},
            {"op": "replace", "path": "/@graph/0/name", "value": "Edited"},
        ],
    )
    assert any(
        i.code == "documents.patch_retained_changed" for i in changed.report.issues
    )
    removed = _apply(document, [{"op": "remove", "path": "/@graph/0/code"}])
    assert not removed.report.verified and removed.document is None


def test_native_rfc_tests_pointer_errors_and_source_snapshot_kinds() -> None:
    _backend()
    document = _document()
    document["@context"]["payload"] = {"@id": EX + "payload", "@type": "@json"}
    document["@graph"][0]["payload"] = {"flag": True, "number": 1, "a/b~c": "retained"}
    edited = {"op": "replace", "path": "/@graph/0/name", "value": "Edited"}
    good = _apply(
        document,
        [
            {"op": "test", "path": "/@graph/0/payload/number", "value": 1.0},
            {"op": "test", "path": "/@graph/0/payload/a~1b~0c", "value": "retained"},
            edited,
        ],
    )
    assert good.report.verified
    bad = _apply(
        document, [{"op": "test", "path": "/@graph/0/payload/flag", "value": 1}, edited]
    )
    assert any(
        i.code == "documents.patch_precondition_failed" for i in bad.report.issues
    )
    expected = deepcopy(document)
    expected["@graph"][0]["payload"]["number"] = 1.0
    assert not _apply(document, [edited], expected_base=expected).report.verified
    for path in ("invalid", "/~2", "/@graph/999/name"):
        invalid = _apply(document, [{"op": "replace", "path": path, "value": "Edited"}])
        assert invalid.report.outcome == "input_error" and invalid.document is None
    unknown = _apply(document, [{"op": "move", "path": "/name", "from": "/other"}])
    assert unknown.report.outcome == "unverified" and unknown.document is None


def test_callback_capture_and_paid_findings_survive_limits() -> None:
    _backend()
    document = _document()
    document["@context"] = EX + "context"
    context = {"name": EX + "name", "code": EX + "code"}
    edits: list[dict[str, object]] = [
        {"op": "replace", "path": "/@graph/0/name", "value": "Edited"}
    ]
    expected = [ContextExpectation(("@graph", 0, "name"), EX + "name")]
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        calls.append(url)
        document.clear()
        edits.clear()
        expected.clear()
        return {
            "document": {"@context": context},
            "documentUrl": EX + "cdn/context",
            "contextUrl": None,
        }

    result = _apply(
        document, edits, after_expectations=expected, document_loader=loader
    )
    assert result.report.verified and calls == [EX + "context"]
    assert isinstance(result.document, dict)
    assert result.document["@graph"][0]["name"] == "Edited"
    paid = False
    for cap in range(1500, 25000, 900):
        limited = _apply(
            _document(),
            [{"op": "replace", "path": "/@graph/0/name", "value": "Edited"}],
            after_expectations=(
                ContextExpectation(("@graph", 0, "name"), EX + "wrong"),
            ),
            limits=Limits(max_work=cap),
        )
        if limited.after_form is not None:
            known = [i for i in limited.after_form.report.issues if i.kind == "finding"]
            if known:
                assert all(i in limited.report.issues for i in known)
                if any(i.kind == "exhausted" for i in limited.report.issues):
                    paid = True
    assert paid
    assert _apply(
        _document(), [{"op": "replace", "path": "/@graph/0/name", "value": "Edited"}]
    ).report.verified
