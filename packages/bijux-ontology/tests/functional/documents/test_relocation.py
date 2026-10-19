"""Preserving resolved identity on relocation also retains selected source form."""

import importlib.util
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from rdflib import Literal, URIRef

from bijux_ontology.documents.export import write_export
from bijux_ontology.documents.forms.types import FormRequirement
from bijux_ontology.documents.relocation import (
    DocumentRelocation,
    export_relocation,
    relocate_document,
)
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.resources import OfflineDocument, OfflineDocumentLoader
from bijux_ontology.jsonld.selectors.types import ArrayItems, Property, SourceSelector
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf

OLD = "https://old.example/records/document.jsonld"
NEW = "https://new.example/moved/document.jsonld"


def document() -> dict[str, Any]:
    """Use original relative references and a root graph array."""
    return {
        "@context": "context.jsonld",
        "@graph": [
            {"@id": "one", "ref": {"@id": "two"}},
            {"@id": "two", "name": "Two"},
        ],
    }


def resources() -> OfflineDocumentLoader:
    """The context's effective URL controls its own relative imports."""
    return OfflineDocumentLoader(
        {
            "https://old.example/records/context.jsonld": OfflineDocument(
                {
                    "@context": {
                        "@import": "./core.jsonld",
                        "ref": {"@id": "urn:ref", "@type": "@id"},
                    }
                },
                effective_url="https://cdn.example/releases/context.jsonld",
            ),
            "https://cdn.example/releases/core.jsonld": {
                "@context": {"name": "urn:name"}
            },
        }
    )


def forms() -> tuple[FormRequirement, ...]:
    """Require the actual root graph array and reference-only relationship."""
    return (
        FormRequirement(SourceSelector(()), shape="object", required_keys=("@graph",)),
        FormRequirement(SourceSelector((Property("@graph"),)), shape="array"),
        FormRequirement(
            SourceSelector((Property("@graph"), ArrayItems())),
            shape="object",
            required_keys=("@id",),
        ),
        FormRequirement(
            SourceSelector(
                (Property("@graph"), ArrayItems(), Property("ref", minimum=0))
            ),
            embedding="reference",
        ),
    )


def expectations() -> tuple[ContextExpectation, ...]:
    """Retain actual resolved identity and a selected literal occurrence."""
    return (
        ContextExpectation(
            ("@graph", 0, "ref"),
            "urn:ref",
            value_kind="iri",
            identifier="https://old.example/records/two",
        ),
        ContextExpectation(("@graph", 1, "name"), "urn:name", value_kind="literal"),
    )


def test_actual_relative_context_effective_imports_and_flat_form_relocate() -> None:
    original = document()
    before = deepcopy(original)
    moved = relocate_document(
        original,
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=forms(),
        expectations=expectations(),
        document_loader=resources(),
    )
    assert original == before
    if importlib.util.find_spec("pyld") is None:
        assert moved.report.outcome == "unverified" and moved.document is None
        assert "[jsonld]" in moved.report.issues[0].message
        return
    assert moved.report.verified, moved.report.to_dict()
    assert moved.before_form is not None and moved.after_form is not None
    assert moved.before_source is moved.before_form.source
    assert moved.after_source is moved.after_form.source
    assert moved.before_source is not None and moved.after_source is not None
    assert moved.rdf_report is not None and moved.rdf_report.verified
    assert moved.usage_report is not None and moved.usage_report.verified
    assert compare_rdf(moved.before_source, moved.after_source).verified
    output = cast(dict[str, Any], moved.document)
    assert output["@graph"] == original["@graph"]
    assert output["@context"] == [
        {"@base": OLD},
        "https://old.example/records/context.jsonld",
    ]
    assert output["@graph"][0]["ref"] == {"@id": "two"}
    assert (
        URIRef("https://old.example/records/one"),
        URIRef("urn:ref"),
        URIRef("https://old.example/records/two"),
    ) in moved.after_source.graph()


def test_owned_candidate_export_and_failed_write_preserve_inputs_and_destinations(
    tmp_path: Path,
) -> None:
    pytest.importorskip("pyld")
    original = document()
    result = relocate_document(
        original,
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=forms(),
        document_loader=resources(),
    )
    assert result.report.verified
    copy = cast(dict[str, Any], result.document)
    copy["@graph"][0]["@id"] = "mutated"
    assert cast(dict[str, Any], result.document)["@graph"][0]["@id"] == "one"
    with pytest.raises(InputError):
        DocumentRelocation()
    with pytest.raises(InputError):
        replace(result, document_json='{"@id":"urn:forged"}')
    published = export_relocation(result)
    assert published.content is not None
    assert json.loads(published.content) == result.document
    target = tmp_path / "relocated.jsonld"
    write_export(published, target)
    assert target.read_bytes() == published.content
    with pytest.raises(ResourceError):
        write_export(published, target)
    assert target.read_bytes() == published.content
    assert original == document()
    strict = (
        FormRequirement(
            SourceSelector(()), shape="object", allowed_keys=("@id", "urn:p")
        ),
    )
    rejected = relocate_document(
        {"@id": "urn:s", "urn:p": "v"},
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=strict,
    )
    assert rejected.before_form is not None and rejected.before_form.report.verified
    assert (
        rejected.after_form is not None
        and rejected.after_form.report.outcome == "disagreement"
    )
    assert rejected.document is None
    with pytest.raises(InputError):
        write_export(export_relocation(rejected), target, overwrite=True)
    assert target.read_bytes() == published.content


def test_null_scoped_protected_or_new_identity_requests_cannot_claim_preservation() -> (
    None
):
    pytest.importorskip("pyld")
    required = (FormRequirement(SourceSelector(()), shape="object"),)
    for unsupported_context in (
        None,
        [None, {"p": "urn:p"}],
        {"p": {"@id": "urn:p", "@context": {"@base": "elsewhere/"}}},
    ):
        result = relocate_document(
            {"@context": unsupported_context, "@id": "one", "p": "v"},
            old_url=OLD,
            new_url=NEW,
            policy="pin-old-base",
            requirements=required,
        )
        assert result.report.outcome == "unverified" and result.document is None
    protected = relocate_document(
        {
            "@context": [{"@protected": True, "p": "urn:old"}, {"p": "urn:new"}],
            "@id": "urn:s",
            "p": "v",
        },
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=required,
    )
    assert protected.report.outcome == "input_error" and protected.document is None
    migration = relocate_document(
        document(),
        old_url=OLD,
        new_url=NEW,
        policy=cast(Any, "new-identity"),
        requirements=forms(),
        document_loader=resources(),
    )
    assert migration.report.outcome == "unverified" and migration.document is None
    for context in (
        {"@base": "child/", "p": {"@id": "urn:p", "@type": "@id"}},
        {"@vocab": "./vocab/"},
    ):
        valid = relocate_document(
            {"@context": context, "@id": "one", "p": "two"},
            old_url=OLD,
            new_url=NEW,
            policy="pin-old-base",
            requirements=required,
        )
        assert valid.report.verified, valid.report.to_dict()


def test_loader_callbacks_cannot_change_captured_document_or_obligations() -> None:
    pytest.importorskip("pyld")
    original = document()
    required = list(forms())
    selected = list(expectations())
    plain = resources()
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Mutate caller-owned values only after the preparation boundary."""
        calls.append(url)
        original["@graph"][0]["ref"]["@id"] = "changed-by-loader"
        required.clear()
        selected.clear()
        return plain(url, options)

    moved = relocate_document(
        original,
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=required,
        expectations=selected,
        document_loader=loader,
    )
    assert moved.report.verified, moved.report.to_dict()
    assert cast(dict[str, Any], moved.document)["@graph"][0]["ref"]["@id"] == "two"
    assert calls == [
        "https://old.example/records/context.jsonld",
        "https://cdn.example/releases/core.jsonld",
    ]
    assert moved.before_form is not None and moved.before_form.form_report.checked == 4
    assert moved.usage_report is not None and moved.usage_report.checked == 2


def test_metadata_bounds_and_paid_raw_failure_prefix_use_one_budget() -> None:
    calls: list[str] = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """An early declaration failure must never reach a loader."""
        calls.append(url)
        return resources()(url, options)

    huge = relocate_document(
        document(),
        old_url="https://old.example/" + "x" * 100000,
        new_url=NEW,
        policy="pin-old-base",
        requirements=forms(),
        document_loader=loader,
        limits=Limits(max_bytes=1000),
    )
    assert huge.report.outcome == "exhausted" and huge.old_url is None
    assert huge.document is None and not calls
    invalid = relocate_document(
        document(),
        old_url=cast(Any, Literal(OLD)),
        new_url=NEW,
        policy="pin-old-base",
        requirements=forms(),
        document_loader=loader,
    )
    assert invalid.report.outcome == "input_error" and not calls
    empty = relocate_document(
        document(),
        old_url=OLD,
        new_url=NEW,
        policy="pin-old-base",
        requirements=(),
        document_loader=loader,
    )
    assert empty.report.outcome == "unverified" and not calls
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    exhausted = relocate_document(
        document(),
        old_url=OLD,
        new_url=NEW,
        policy=cast(Any, "unsupported"),
        requirements=forms(),
        budget=spent,
    )
    assert exhausted.report.outcome == "exhausted"
    if importlib.util.find_spec("pyld") is None:
        return
    wrong = (FormRequirement(SourceSelector(()), shape="array"),)
    paid = 0
    for work in range(1000, 18000, 500):
        result = relocate_document(
            document(),
            old_url=OLD,
            new_url=NEW,
            policy="pin-old-base",
            requirements=wrong,
            document_loader=resources(),
            limits=Limits(max_work=work),
        )
        known = any(i.code == "documents.form_shape" for i in result.report.issues)
        stopped = any(i.kind == "exhausted" for i in result.report.issues)
        paid += known and stopped
        if known:
            assert result.before_form is not None
    assert paid > 0
