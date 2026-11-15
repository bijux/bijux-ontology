"""Unsupported OAK runtimes fail explicitly without importing an optional host."""

import pytest
from rdflib import Graph

from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology_adapters.oak import preparation


@pytest.mark.parametrize("runtime", [(3, 11), (3, 15)])
def test_unsupported_oak_runtime_keeps_core_available(
    monkeypatch: pytest.MonkeyPatch, runtime: tuple[int, int]
) -> None:
    view = VocabularyView(load_vocabulary(Graph()))

    def reject_import(name: str) -> None:
        raise AssertionError(f"Unsupported runtime imported {name}")

    monkeypatch.setattr(preparation.sys, "version_info", (*runtime, 0, "final", 0))
    monkeypatch.setattr(preparation, "import_module", reject_import)
    result = preparation.as_oak(view, registry=IdentifierRegistry(()))
    assert result.adapter is None
    assert not result.report.verified
    assert [issue.code for issue in result.report.issues] == [
        "adapter.oak_python_unsupported"
    ]
