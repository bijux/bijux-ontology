"""Original flat/nested documents retain explicit field associations and raw truth."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.declarations.bindings import BindingIdentity
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.execution import operations as execution_operations
from bijux_ontology.models.emission.observations import SerializedFieldBinding
from bijux_ontology.models.emission.serialization import check_serialized
from bijux_ontology.models.emission.types import ModelUsage

EX = Namespace("urn:serializer-bindings:")


def _models() -> tuple[ModelDeclaration, ...]:
    return (
        ModelDeclaration(
            "study",
            (str(EX.Study),),
            (
                FieldDeclaration(
                    "title",
                    str(EX.title),
                    (ValueDeclaration("literal", str(XSD.string)),),
                ),
                FieldDeclaration(
                    "abstract",
                    str(EX.abstract),
                    (ValueDeclaration("literal", str(XSD.string)),),
                    optional=True,
                ),
            ),
        ),
        ModelDeclaration(
            "person",
            (str(EX.Person),),
            (
                FieldDeclaration(
                    "name",
                    str(EX.name),
                    (ValueDeclaration("literal", str(XSD.string)),),
                ),
            ),
        ),
    )


def _vocabulary() -> Graph:
    graph = Graph()
    for cls in (EX.Study, EX.Person):
        graph.add((cls, RDF.type, OWL.Class))
    for prop in (EX.title, EX.name, EX.abstract, EX.child, EX.changed):
        graph.add((prop, RDF.type, RDF.Property))
    return graph


def test_flat_graph_bindings_retain_independent_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = {
        "title": str(EX.title),
        "name": str(EX.name),
        "abstract": str(EX.abstract),
    }
    document = {
        "@context": context,
        "@graph": [
            {"@id": str(EX.study), "@type": str(EX.Study), "title": "Authored study"},
            {"@id": str(EX.person), "@type": str(EX.Person), "name": "Authored person"},
        ],
    }
    before = deepcopy(document)
    usages = (
        ModelUsage("study", reference=str(EX.study), graph=None, entity=True),
        ModelUsage("person", reference=str(EX.person), graph=None, entity=True),
    )
    bindings = (
        SerializedFieldBinding(
            BindingIdentity("Study", ("title",)),
            usages[0],
            "title",
            ("@graph", 0, "title"),
        ),
        SerializedFieldBinding(
            BindingIdentity("Person", ("name",)),
            usages[1],
            "name",
            ("@graph", 1, "name"),
        ),
    )
    result = check_serialized(
        document, _models(), usages, vocabulary=_vocabulary(), bindings=bindings
    )
    assert result.report.verified, result.report.to_dict()
    assert result.output == document == before and result.document == document
    assert result.observed is not None
    assert all(item.completed for item in result.observed.bindings)
    assert {item.identity for item in result.contract_evidence.bindings} == {
        item.identity for item in bindings
    }
    assert result.observed.declarations[0].fields[0].predicate == str(EX.title)
    assert (
        "unassociated model field: study.abstract" in result.observed.report.exclusions
    )
    changed = deepcopy(document)
    cast(dict[str, str], changed["@context"])["title"] = str(EX.changed)
    wrong = check_serialized(
        changed, _models(), usages, vocabulary=_vocabulary(), bindings=bindings
    )
    assert wrong.report.outcome == "disagreement"
    assert wrong.observed is not None
    assert wrong.observed.declarations[0].fields[0].predicate == str(EX.changed)
    assert any(
        issue.code == "serialization.binding_predicate_mismatch"
        for issue in wrong.report
    )
    absent = SerializedFieldBinding(
        BindingIdentity("Study", ("abstract",)),
        usages[0],
        "abstract",
        ("@graph", 0, "abstract"),
    )
    unknown = check_serialized(
        document,
        _models(),
        usages,
        vocabulary=_vocabulary(),
        bindings=(*bindings, absent),
    )
    assert unknown.observed is not None and not unknown.report.verified
    assert unknown.observed.bindings[-1].exclusion == "not serialized in this sample"
    assert not unknown.observed.bindings[-1].completed
    unrelated = replace(bindings[0], usage=replace(usages[0], reference=str(EX.person)))
    unresolved = check_serialized(
        document,
        _models(),
        (unrelated.usage, usages[1]),
        vocabulary=_vocabulary(),
        bindings=(unrelated,),
    )
    assert (
        unresolved.observed is not None
        and not unresolved.observed.bindings[0].completed
    )

    empty_document = deepcopy(document)
    cast(list[dict[str, object]], empty_document["@graph"])[0]["title"] = []
    empty = check_serialized(
        empty_document, _models(), usages, vocabulary=_vocabulary(), bindings=bindings
    )
    assert empty.observed is not None
    assert not empty.observed.bindings[0].completed
    assert empty.observed.bindings[0].exclusion is not None

    from bijux_ontology.interfaces.execution.executor import WorkerExecutor

    request: dict[str, object] = {
        "document": before,
        "models": [
            {
                "identifier": name,
                "types": [str(getattr(EX, kind))],
                "fields": [
                    {
                        "name": field,
                        "predicate": str(getattr(EX, field)),
                        "values": [{"kind": "literal", "datatype": str(XSD.string)}],
                    }
                ],
            }
            for name, kind, field in (
                ("study", "Study", "title"),
                ("person", "Person", "name"),
            )
        ],
        "usages": [
            {
                "model": name,
                "reference": str(getattr(EX, name)),
                "graph": None,
                "entity": True,
            }
            for name in ("study", "person")
        ],
        "bindings": [
            {
                "identity": {"owner": owner, "path": [field]},
                "usage": {
                    "model": name,
                    "reference": str(getattr(EX, name)),
                    "graph": None,
                    "entity": True,
                },
                "field": field,
                "path": ["@graph", index, field],
            }
            for index, (name, owner, field) in enumerate(
                (("study", "Study", "title"), ("person", "Person", "name"))
            )
        ],
        "vocabulary": {
            "source": _vocabulary().serialize(format="turtle"),
            "format": "turtle",
        },
    }
    isolated = WorkerExecutor(artifacts=tmp_path).run("emission", request)
    assert isolated.report.verified, isolated.report.to_dict()
    assert isinstance(isolated.data, dict)
    assert len(isolated.data["declarations"]) == 2
    assert [item["owner"] for item in isolated.data["bindings"]] == ["Study", "Person"]

    def exhaust_publication(report: Report, budget: Budget) -> dict[str, object]:
        """Exhaust real response storage after independent semantic work completed."""
        budget.consume("bytes", budget.remaining("bytes") + 1)
        return report.to_dict()

    monkeypatch.setattr(execution_operations, "report_to_data", exhaust_publication)
    request["document"] = changed
    retained, data = execution_operations.execute_operation(
        "emission", request, Limits(), lambda report: None
    )
    assert data is None and retained.outcome == "exhausted"
    assert any(
        issue.code == "serialization.binding_predicate_mismatch" for issue in retained
    )


def test_nested_bindings_observe_native_fields() -> None:
    models = _models()
    parent = replace(
        models[0],
        fields=(
            models[0].fields[0],
            FieldDeclaration(
                "child", str(EX.child), (ValueDeclaration("object", model="person"),)
            ),
        ),
    )
    document = {
        "@context": {
            "title": str(EX.title),
            "child": str(EX.child),
            "name": str(EX.name),
        },
        "@id": str(EX.study),
        "@type": str(EX.Study),
        "title": "Study",
        "child": {"@id": str(EX.person), "@type": str(EX.Person), "name": "Person"},
    }
    usages = (ModelUsage("study"), ModelUsage("person", ("child",)))
    bindings = (
        SerializedFieldBinding(
            BindingIdentity("Study", ("title",)), usages[0], "title", ("title",)
        ),
        SerializedFieldBinding(
            BindingIdentity("Person", ("name",)), usages[1], "name", ("child", "name")
        ),
    )
    result = check_serialized(
        document,
        (parent, models[1]),
        usages,
        vocabulary=_vocabulary(),
        bindings=bindings,
    )
    assert result.report.verified, result.report.to_dict()
    assert result.observed is not None and len(result.observed.declarations) == 2
    assert all(item.completed for item in result.observed.bindings)
    assert result.output == document
