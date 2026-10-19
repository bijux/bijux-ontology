"""Prepared fields avoid whole-document work and own changing remote resources."""

from typing import cast

import pytest
from pyld import jsonld  # type: ignore[import-untyped]

from bijux_ontology.jsonld.contracts.expectations import ContextExpectation
from bijux_ontology.jsonld.usages.checking import prepare_usages

EX = "https://example.org/"


def test_many_flat_usages_expand_only_their_proven_independent_contributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    expanded_sizes: list[int] = []
    actual_expand = jsonld.JsonLdProcessor.expand

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Changing remote content must be read once throughout preparation."""
        calls.append(url)
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": {
                "@context": {"@vocab": EX if len(calls) == 1 else EX + "changed/"}
            },
        }

    def recording_expand(
        processor: object, document: object, *args: object, **kwargs: object
    ) -> object:
        """Measure supplied document work without replacing processor semantics."""
        if isinstance(document, dict):
            expanded_sizes.append(sum(not key.startswith("@") for key in document))
        return cast(object, actual_expand(processor, document, *args, **kwargs))

    monkeypatch.setattr(jsonld.JsonLdProcessor, "expand", recording_expand)
    fields = 400
    document = {
        "@context": EX + "context",
        **{f"p{index}": str(index) for index in range(fields)},
    }
    expectations = [
        ContextExpectation((f"p{index}",), EX + f"p{index}") for index in range(fields)
    ]
    prepared = prepare_usages(document, document_loader=loader)
    assert prepared.check(expectations).verified
    assert sum(expanded_sizes) == fields * 2
    assert calls == [EX + "context"]
    before = list(expanded_sizes)
    assert prepared.check(expectations).verified
    assert expanded_sizes == before


def test_optional_empty_predicate_subcheck_remains_distinct_from_required_scope() -> (
    None
):
    prepared = prepare_usages({"@id": EX + "root", "@type": EX + "Record"})
    assert prepared.root_nodes[0].types == (EX + "Record",)
    assert prepared.check([]).exit_code() == 2
    optional = prepared.check([], allow_empty=True)
    assert optional.outcome == "not_applicable"
    assert optional.exit_code() == 0
    assert not optional.verified


def test_owned_original_projection_keeps_budget_and_processing_identity() -> None:
    """Actual directional projections remain independent of the shared expansion."""
    from copy import copy

    from bijux_ontology.diagnostics.errors import InputError
    from bijux_ontology.execution.limits import Budget
    from bijux_ontology.jsonld.processing.conversion import (
        _convert_original_expansion,
        convert_to_rdf,
    )
    from bijux_ontology.jsonld.processing.expansion import capture_original_expansion
    from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader

    document = {
        "@id": "item",
        "@context": EX + "context",
        "label": {"@value": "text", "@language": "en", "@direction": "rtl"},
    }
    loader = OfflineDocumentLoader(
        {EX + "context": {"@context": {"label": EX + "label"}}}
    )
    operation = Budget()
    original = capture_original_expansion(
        document, base=EX + "first/", document_loader=loader, budget=operation
    )
    i18n = _convert_original_expansion(
        original, budget=operation, direction="i18n-datatype"
    )
    compound = _convert_original_expansion(
        original, budget=operation, direction="compound-literal"
    )
    assert i18n.report.verified and compound.report.verified
    assert EX + "first/item" in i18n.nquads and EX + "first/item" in compound.nquads
    assert i18n.nquads != compound.nquads
    other_loader = OfflineDocumentLoader(
        {EX + "context": {"@context": {"label": EX + "other-label"}}}
    )
    changed_resources = convert_to_rdf(
        document,
        base=EX + "first/",
        document_loader=other_loader,
        direction="i18n-datatype",
    )
    assert changed_resources.report.verified
    assert f"<{EX}other-label>" in changed_resources.nquads
    assert f"<{EX}other-label>" not in i18n.nquads
    document["@id"] = "changed"
    assert original._document != document
    changed = convert_to_rdf(
        document, base=EX + "second/", document_loader=loader, direction="i18n-datatype"
    )
    assert changed.report.verified and EX + "second/changed" in changed.nquads
    with pytest.raises(InputError, match="operation budget"):
        _convert_original_expansion(
            original, budget=Budget(), direction="i18n-datatype"
        )
    incompatible = copy(original)
    object.__setattr__(incompatible, "_options", (("processingMode", "json-ld-1.0"),))
    with pytest.raises(InputError, match="different processing options"):
        _convert_original_expansion(incompatible, budget=operation)
