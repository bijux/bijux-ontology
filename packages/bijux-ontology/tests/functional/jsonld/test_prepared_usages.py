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
