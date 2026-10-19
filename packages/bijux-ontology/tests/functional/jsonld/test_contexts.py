"""Protect effective mappings and the consumer's explicit resolution boundary."""

from copy import deepcopy
from pathlib import Path

import pytest
from pyld import jsonld  # type: ignore[import-untyped]

from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.processing.expansion import expand_document
from bijux_ontology.jsonld.processing.resources import (
    ContextInputError,
    OfflineDocumentLoader,
    context_document_loader,
)

EX = "https://example.org/"


def test_operation_owns_resources_and_reads_a_changing_loader_once() -> None:
    calls = 0

    def changing_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {
                "@context": {"name": EX + ("wrong" if calls == 1 else "name")}
            },
        }

    report = check_context(
        {"@context": EX + "context", "name": "Actual"},
        [ContextExpectation(("name",), EX + "name")],
        document_loader=changing_loader,
    )
    assert calls == 1
    assert report.exit_code() == 1
    assert report.issues[0].actual == EX + "wrong"
    document = {"@context": {"name": EX + "name"}}
    loader = OfflineDocumentLoader({EX + "context": document})
    document["@context"]["name"] = EX + "mutated"
    returned = loader(EX + "context", {})
    assert returned["document"] == {"@context": {"name": EX + "name"}}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numbers_are_invalid_json_inputs(value: float) -> None:
    report = check_context(
        {"@context": {"value": EX + "value"}, "value": value},
        [ContextExpectation(("value",), EX + "value", value_kind="literal")],
    )
    assert report.exit_code() == 2
    assert not report.verified
    assert report.issues[0].kind == "input_error"
    assert report.issues[0].code == "context.input_error"


def test_scoped_composed_mappings_keywords_and_original_values() -> None:
    document = {
        "@context": [
            {"@vocab": EX, "identity": "@id", "kind": "@type"},
            {
                "Record": {"@id": EX + "Record", "@context": {"title": EX + "heading"}},
                "child": {"@id": EX + "child", "@context": {"title": EX + "label"}},
                "name": {"@id": EX + "name", "@language": "EN-gb"},
                "link": {"@id": EX + "link", "@type": "@id"},
            },
        ],
        "identity": "record",
        "kind": "Record",
        "title": "Parent",
        "name": "Colour",
        "link": "other",
        "child": {"title": "Child"},
    }
    before = deepcopy(document)
    report = check_context(
        document,
        [
            ContextExpectation(("identity",), "@id", value_kind="iri"),
            ContextExpectation(("kind",), "@type", value_kind="iri"),
            ContextExpectation(("title",), EX + "heading", value_kind="literal"),
            ContextExpectation(("child", "title"), EX + "label"),
            ContextExpectation(("name",), EX + "name", language="en-GB"),
            ContextExpectation(("link",), EX + "link", value_kind="iri"),
        ],
        base=EX,
    )
    assert report.verified, report.to_dict()
    assert document == before
    expanded = expand_document(document, base=EX)
    assert expanded[0]["@id"] == EX + "record"
    assert expanded[0][EX + "link"] == [{"@id": EX + "other"}]
    different = check_context(
        document, [ContextExpectation(("name",), EX + "name", language="fr")], base=EX
    )
    assert [issue.code for issue in different.issues] == ["context.language_mismatch"]


def test_mapping_drops_coercions_and_all_list_values_are_checked() -> None:
    document = {
        "@context": {"title": EX + "wrong", "values": EX + "values"},
        "title": "Name",
        "missing": "Must survive",
        "values": [{"@value": "one", "@language": "en"}, {"@id": EX + "two"}],
    }
    report = check_context(
        document,
        [
            ContextExpectation(("title",), EX + "title"),
            ContextExpectation(("missing",), EX + "missing"),
            ContextExpectation(("values",), EX + "values", value_kind="literal"),
        ],
    )
    assert report.checked == 3
    assert {issue.code for issue in report.issues} == {
        "context.mapping_mismatch",
        "context.property_dropped",
        "context.term_dropped",
        "context.value_kind_mismatch",
    }
    assert report.exit_code() == 1


def test_lists_reverse_values_and_datatype_are_observed() -> None:
    datatype = "http://www.w3.org/2001/XMLSchema#decimal"
    document = {
        "@context": {
            "parts": {"@id": EX + "parts", "@container": "@list"},
            "parent": {"@reverse": EX + "child", "@type": "@id"},
            "size": {"@id": EX + "size", "@type": datatype},
            "count": EX + "count",
        },
        "parts": ["left", "right"],
        "parent": EX + "parent",
        "size": "1.5",
        "count": 2,
    }
    expectations = [
        ContextExpectation(
            ("parts",), EX + "parts", value_kind="literal", container="list"
        ),
        ContextExpectation(("parent",), EX + "child", value_kind="iri", reverse=True),
        ContextExpectation(("size",), EX + "size", datatype=datatype),
        ContextExpectation(
            ("count",),
            EX + "count",
            datatype="http://www.w3.org/2001/XMLSchema#integer",
        ),
    ]
    assert check_context(document, expectations).verified
    wrong = check_context(document, [ContextExpectation(("parent",), EX + "child")])
    assert [issue.code for issue in wrong.issues] == ["context.reverse_mismatch"]


def test_offline_import_keeps_logical_url_and_application_base(tmp_path: Path) -> None:
    imported = tmp_path / "imported.json"
    imported.write_text(
        '{"@context":{"item":{"@id":"https://example.org/item","@type":"@id"}}}'
    )
    documents = {
        EX + "contexts/main": {"@context": {"@import": "imported"}},
        EX + "contexts/imported": imported,
    }
    before = deepcopy(documents)
    loader = OfflineDocumentLoader(documents)
    document = {"@context": EX + "contexts/main", "@id": "record", "item": "child"}
    expanded = expand_document(document, base=EX + "data/", document_loader=loader)
    assert expanded == [
        {"@id": EX + "data/record", EX + "item": [{"@id": EX + "data/child"}]}
    ]
    assert documents == before
    assert check_context(
        document,
        [ContextExpectation(("item",), EX + "item", value_kind="iri")],
        base=EX + "data/",
        document_loader=loader,
    ).verified
    assert (
        loader(EX + "contexts/imported", {})["documentUrl"] == EX + "contexts/imported"
    )


def test_missing_resources_never_fall_back_to_the_global_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def network_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Global loader must not be used")

    monkeypatch.setattr(jsonld, "_default_document_loader", network_forbidden)
    report = check_context(
        {"@context": EX + "absent", "title": "Name"},
        [ContextExpectation(("title",), EX + "title")],
    )
    assert report.exit_code() == 2
    assert report.issues[0].kind == "input_error"
    with pytest.raises(ContextInputError, match="No supplied JSON-LD document"):
        expand_document({"@context": EX + "absent"})


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        pytest.param("{", "input_error", id="malformed"),
        pytest.param("[" * 1100 + "0" + "]" * 1100, "exhausted", id="depth"),
        pytest.param("[" + "9" * 5000 + "]", "input_error", id="integer"),
    ],
)
def test_snapshot_decoder_failures_have_controlled_outcomes(
    tmp_path: Path, payload: str, kind: str
) -> None:
    snapshot = tmp_path / "context.json"
    snapshot.write_text(payload, encoding="utf-8")
    report = check_context(
        {"@context": EX + "context", "name": "Name"},
        [ContextExpectation(("name",), EX + "name")],
        document_loader=OfflineDocumentLoader({EX + "context": snapshot}),
    )
    assert report.exit_code() == 2
    assert report.issues[0].kind == kind
    assert not report.verified


def test_empty_missing_and_null_usages_cannot_be_reported_as_verified() -> None:
    document = {"@context": {"name": EX + "name"}, "name": None}
    assert check_context(document, []).exit_code() == 2
    missing = check_context(document, [ContextExpectation(("absent",), EX + "absent")])
    assert missing.exit_code() == 2
    null = check_context(document, [ContextExpectation(("name",), EX + "name")])
    assert not null.verified
    assert null.issues[0].kind == "unverified"
    unsupported = check_context(document, [ContextExpectation(("name",), "@graph")])
    assert unsupported.exit_code() == 0
    assert unsupported.exit_code(strict=True) == 1
    assert not unsupported.verified
    assert unsupported.issues[0].code == "context.unsupported_check"


def test_loader_programmer_failures_propagate_and_aliased_values_stay_separate() -> (
    None
):
    def broken_loader(url: str, options: dict[str, object]) -> dict[str, object]:
        raise RuntimeError("consumer defect")

    with pytest.raises(RuntimeError, match="consumer defect"):
        expand_document({"@context": EX + "context"}, document_loader=broken_loader)
    with pytest.raises(RuntimeError, match="consumer defect"):
        context_document_loader(broken_loader)(EX + "context", {})
    document = {
        "@context": {
            "name": {"@id": EX + "name", "@language": "en"},
            "alias": {"@id": EX + "name", "@type": "@id"},
        },
        "name": "Colour",
        "alias": EX + "record",
    }
    report = check_context(
        document,
        [
            ContextExpectation(
                ("name",), EX + "name", value_kind="literal", language="en"
            ),
            ContextExpectation(("alias",), EX + "name", value_kind="iri"),
        ],
    )
    assert report.verified, report.to_dict()


def test_shared_loader_preserves_literal_json_and_bases_context_imports() -> None:
    literal = {"@context": {"@import": "literal-context"}, "@import": "literal-import"}
    source = {
        "@context": {"payload": {"@id": EX + "payload", "@type": "@json"}},
        "@id": EX + "record",
        "payload": literal,
    }
    context = {"@context": {"@import": "imported"}}
    snapshots = {EX + "record": source, EX + "contexts/main": context}
    before = deepcopy(snapshots)
    loader = context_document_loader(OfflineDocumentLoader(snapshots))
    assert loader(EX + "record", {})["document"] == source
    assert loader(EX + "contexts/main", {})["document"] == {
        "@context": {"@import": EX + "contexts/imported"}
    }
    expanded = expand_document(EX + "record", document_loader=loader)
    assert expanded[0][EX + "payload"] == [{"@type": "@json", "@value": literal}]
    assert snapshots == before


@pytest.mark.parametrize("conflict", ["document", "contextUrl", "none"])
def test_logical_resource_aliases_are_equal_or_explicitly_rejected(
    conflict: str,
) -> None:
    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Different request aliases cannot redefine the same logical resource."""
        other = url.endswith("second")
        return {
            "documentUrl": EX + "logical",
            "contextUrl": EX + "header" if other and conflict == "contextUrl" else None,
            "document": {
                "@context": {
                    "p": EX + ("other" if other and conflict == "document" else "p")
                }
            },
        }

    document = {"@context": [EX + "first", EX + "second"], "p": "value"}
    if conflict == "none":
        assert expand_document(document, document_loader=loader)[0][EX + "p"] == [
            {"@value": "value"}
        ]
    else:
        with pytest.raises(ContextInputError, match="Conflicting supplied"):
            expand_document(document, document_loader=loader)


def test_drop_policy_is_exact_retains_observations_and_never_hides_unknown_terms() -> (
    None
):
    from bijux_ontology.jsonld.checking import prepare_usages
    from bijux_ontology.jsonld.processing.conversion import convert_to_rdf

    document = {"@context": {"p": EX + "p"}, "p": "kept", "ignored": "omitted"}
    prepared = prepare_usages(document, allowed_drops=frozenset({"ignored"}))
    report = prepared.check([ContextExpectation(("p",), EX + "p")])
    assert prepared.allowed_drops == frozenset({"ignored"})
    assert report.exit_code() == 0
    assert not report.verified
    assert [(issue.actual, issue.severity) for issue in report.issues] == [
        ("ignored", "info")
    ]
    converted = convert_to_rdf(document, allowed_drops=prepared.allowed_drops)
    assert converted.nquads
    assert converted.report.exit_code() == 0
    assert not converted.report.verified
    unexpected = check_context(
        document | {"misspelt": "x"},
        [ContextExpectation(("p",), EX + "p")],
        allowed_drops=prepared.allowed_drops,
    )
    assert unexpected.exit_code() == 1
    assert {
        issue.actual for issue in unexpected.issues if issue.severity == "error"
    } == {"misspelt"}
    null_mapping = {
        "@context": {"p": EX + "p", "removed": None},
        "p": "kept",
        "removed": "discarded",
    }
    unknown = check_context(
        null_mapping,
        [ContextExpectation(("p",), EX + "p")],
        allowed_drops=frozenset({"removed"}),
    )
    assert [(issue.actual, issue.severity) for issue in unknown.issues] == [
        (None, "error")
    ]


def test_native_processing_defects_withhold_proof_and_preserve_caller_type_errors() -> (
    None
):
    """Actual malformed native input cannot crash or become a semantic success."""
    from bijux_ontology.diagnostics.errors import ResourceError
    from bijux_ontology.jsonld.processing.preparation import prepare_jsonld

    document = {"@context": {"label": {"@id": []}}, "label": "x"}
    with pytest.raises(ResourceError, match="without a semantic input diagnostic"):
        expand_document(document)
    checked = check_context(document, (ContextExpectation(("label",), EX + "label"),))
    assert checked.outcome == "unverified" and not checked.complete
    assert checked.issues[0].code == "context.processor_unverified"
    prepared = prepare_jsonld(document)
    assert prepared.source is None and prepared.report.outcome == "unverified"

    failure = TypeError("consumer callback defect")

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        raise failure

    def dropped(term: str | None) -> None:
        raise failure

    with pytest.raises(TypeError) as caught:
        expand_document({"@context": EX + "context"}, document_loader=loader)
    assert caught.value is failure and failure.__cause__ is not failure
    with pytest.raises(TypeError) as caught:
        expand_document({"unmapped": "x"}, on_property_dropped=dropped)
    assert caught.value is failure and failure.__cause__ is not failure
