"""Maintained flattening preserves RDF while node correspondence stays explicit."""

from copy import deepcopy

import pytest
from rdflib.namespace import XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.jsonld.flattening import flatten_document
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.types import ModelUsage

EX = "https://example.org/"


def test_flat_inventory_binds_actual_models_with_shared_nodes_and_graphs() -> None:
    document = {
        "@context": {"@vocab": EX},
        "@id": "root",
        "child": {"@id": "child", "@type": "Child", "name": "Shared"},
        "other": {"@id": "child"},
        "@reverse": {"parent": [{"@id": "reverse"}]},
        "@included": [
            {"@id": "graph", "@graph": [{"@id": "root", "note": "Named"}]},
            {"@id": "empty", "@graph": []},
        ],
    }
    original = deepcopy(document)
    result = flatten_document(document, base=EX, correspondence=True)
    assert result.report.verified, result.report.to_dict()
    assert result.correspondence_report is not None
    assert result.correspondence_report.verified
    assert "rdf.comparison" in result.report.families
    assert document == original
    root = result.bind(EX + "root")
    assert root.value[EX + "child"] == root.value[EX + "other"]
    assert result.bind(EX + "reverse").value[EX + "parent"] == [{"@id": EX + "root"}]
    assert result.bind(EX + "root", graph=EX + "graph").path[-2:] == ("@graph", 0)
    assert result.bind(EX + "empty").value["@graph"] == []
    child = result.bind(EX + "child")
    assert child.expanded_paths == ((0, EX + "child", 0),)
    model = ModelDeclaration(
        "child",
        (EX + "Child",),
        (
            FieldDeclaration(
                EX + "name",
                EX + "name",
                (ValueDeclaration("literal", str(XSD.string)),),
                emission="guaranteed",
            ),
        ),
    )
    assert check_emission(child.value, [model], [ModelUsage("child")]).verified
    invalid = child.value
    invalid[EX + "name"] = [{"@value": 7}]
    assert not check_emission(invalid, [model], [ModelUsage("child")]).verified
    exported = result.document
    assert exported is not None
    exported.clear()
    child.value.clear()
    document.clear()
    assert result.document and child.value
    with pytest.raises(InputError, match="selected graph"):
        result.bind(EX + "root", graph=EX + "missing")


def test_rdf_preservation_does_not_invent_ambiguous_source_correspondence() -> None:
    document = {EX + "p": [{EX + "name": "Same"}, {EX + "name": "Same"}]}
    result = flatten_document(document, correspondence=True)
    assert result.report.verified
    assert len(result.nodes) == len({node.identifier for node in result.nodes}) == 3
    assert result.correspondence_report is not None
    assert result.correspondence_report.outcome == "unverified"
    assert all(not node.expanded_paths for node in result.nodes)
    assert all(
        issue.location.startswith("expanded-input/")
        for issue in result.correspondence_report.issues
    )
    assert flatten_document(document).correspondence_report is None


def test_flattening_owns_offline_resources_and_effective_document_base() -> None:
    calls = []

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        """Return independent supplied resources with a redirected logical base."""
        calls.append(url)
        return {
            "documentUrl": EX + "data/record.jsonld" if url == EX + "input" else url,
            "contextUrl": None,
            "document": (
                {"@context": EX + "context", "@id": "record", "name": "Text"}
                if url == EX + "input"
                else {"@context": {"name": EX + "name"}}
            ),
        }

    result = flatten_document(EX + "input", document_loader=loader)
    assert result.report.verified
    assert result.bind(EX + "data/record").value[EX + "name"] == [{"@value": "Text"}]
    assert calls == [EX + "input", EX + "context"]
    missing = flatten_document({"@context": EX + "missing", "name": "Text"})
    assert missing.document is None and missing.report.outcome == "input_error"
    bounded = flatten_document({EX + "p": "Text"}, limits=Limits(max_work=1))
    assert bounded.document is None and bounded.report.outcome == "exhausted"
    dropped = flatten_document({"unmapped": "Text"})
    assert dropped.document is None and not dropped.report.verified
    assert "context.property_dropped" in {issue.code for issue in dropped.report.issues}
