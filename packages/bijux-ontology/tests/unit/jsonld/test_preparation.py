"""Captured JSON-LD requires workspace in the caller's actual allowance."""

import pytest

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.contexts.preparation import capture_context_json
from bijux_ontology.jsonld.processing.expansion import expand_document


def test_context_copy_cannot_use_an_unfunded_independent_allowance() -> None:
    context = {f"p{i}": f"urn:p{i}" for i in range(8)}
    with pytest.raises(LimitExceeded):
        capture_context_json(context, Budget(Limits(max_total_bytes=512)))
    owned = capture_context_json(context, Budget())
    context["p0"] = "urn:changed"
    assert owned == {f"p{i}": f"urn:p{i}" for i in range(8)}


def test_expansion_cannot_copy_a_document_with_an_independent_allowance() -> None:
    document = {
        "@id": "urn:captured:subject",
        "urn:captured:values": [{"@value": str(index)} for index in range(2000)],
    }
    with pytest.raises(LimitExceeded):
        expand_document(document, budget=Budget(Limits(max_total_bytes=90_000)))
    expanded = expand_document(document, budget=Budget())
    assert expanded == [document]
