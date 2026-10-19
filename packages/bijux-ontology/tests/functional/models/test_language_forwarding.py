"""Selected direction policies survive supported sample, batch and worker APIs."""

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field, field_serializer
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.interfaces.adapters.pydantic import check_instance
from bijux_ontology.interfaces.execution.executor import WorkerExecutor
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.models.context_probes.checking import probe_context
from bijux_ontology.models.context_probes.types import ContextProbe
from bijux_ontology.models.corpus.checking import (
    SerializerCase,
    check_serializer_corpus,
)
from bijux_ontology.models.corpus.types import BranchCoverage
from bijux_ontology.models.emission import ModelUsage
from bijux_ontology.models.preparation import PreparedModels
from bijux_ontology.models.rdf_emission import RdfModelUsage
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig

EX = Namespace("https://language.example/")


def _model(language: str = "ar", *, key: str | None = None) -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", language=language, direction="rtl"),),
                key=key,
            ),
        ),
    )


def _vocabulary() -> Graph:
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.label, RDF.type, RDF.Property))
    )


def _document(direction: str = "rtl") -> dict[str, object]:
    return {
        "@id": str(EX.root),
        "@type": str(EX.Record),
        "label": {"@value": "اسم", "@language": "ar", "@direction": direction},
        "@context": {"label": str(EX.label)},
    }


def _data() -> list[dict[str, object]]:
    return [
        {
            "identifier": "record",
            "types": [str(EX.Record)],
            "fields": [
                {
                    "name": "label",
                    "predicate": str(EX.label),
                    "values": [
                        {"kind": "literal", "language": "ar", "direction": "rtl"}
                    ],
                }
            ],
        }
    ]


class ArabicRecord(BaseModel):
    calls: ClassVar[list[dict[str, object]]] = []
    identity: str = Field(serialization_alias="@id")
    kind: str = Field(serialization_alias="@type")
    label: str = Field(serialization_alias="title")
    note: str | None = None

    @field_serializer("label")
    def label_json(self, value: str) -> dict[str, str]:
        return {"@value": value, "@language": "ar", "@direction": "rtl"}

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(kwargs)
        return super().model_dump(**kwargs)


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_pydantic_sample_infers_direction_without_a_physical_langstring_promise(
    policy: DirectionPolicy,
) -> None:
    record = ArabicRecord(identity=str(EX.root), kind=str(EX.Record), label="اسم")
    options = {
        "mode": "json",
        "by_alias": True,
        "exclude_none": True,
        "exclude_unset": False,
        "exclude_defaults": False,
    }
    context = {"title": str(EX.label), "note": str(EX.note)}
    ArabicRecord.calls.clear()
    result = check_instance(
        record,
        vocabulary=_vocabulary(),
        context=context,
        options=options,
        rdf_direction=policy,
    )
    assert result.report.verified, result.report.to_dict()
    assert ArabicRecord.calls == [options]
    assert cast(dict[str, object], result.output)["title"] == {
        "@value": "اسم",
        "@language": "ar",
        "@direction": "rtl",
    }
    assert "note" not in cast(dict[str, object], result.output)
    independent = _model("en", key="title")
    wrong = check_instance(
        record,
        vocabulary=_vocabulary(),
        context=context,
        options=options,
        inventory=[independent],
        rdf_direction=policy,
    )
    assert not wrong.report.verified and ArabicRecord.calls == [options, options]
    assert any(issue.code == "emission.value_mismatch" for issue in wrong.report.issues)


def test_context_probe_keeps_absence_distinct_from_unchecked_direction() -> None:
    model = ModelDeclaration(
        "record",
        (),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal", language=ABSENT, direction=ABSENT),),
            ),
        ),
    )
    context = {
        "@language": "ar",
        "@direction": "rtl",
        "label": {
            "@id": str(EX.label),
            "@language": None,
            "@direction": None,
        },
    }
    assert probe_context(context, [ContextProbe(model)]).verified
    changed = {"@language": "ar", "@direction": "rtl", "label": str(EX.label)}
    assert probe_context(changed, [ContextProbe(model)]).outcome == "disagreement"


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_prepared_models_freeze_policy_for_native_and_fresh_batch_checks(
    policy: DirectionPolicy,
) -> None:
    plan = PreparedModels([_model()], _vocabulary(), rdf_direction=policy)
    assert (
        plan.declaration_report.verified and "rdf_direction" in plan.resource_versions
    )
    usages = [ModelUsage("record")]
    outputs = [_document(), _document("ltr"), _document()]
    assert [report.verified for report in plan.check_many(outputs, usages)] == [
        True,
        False,
        True,
    ]
    assert plan.check(outputs[0], usages).verified
    prepared = prepare_jsonld(outputs[0], direction=policy)
    assert prepared.source is not None
    assert plan.check_rdf(prepared.source, [RdfModelUsage("record", EX.root)]).verified
    with pytest.raises(FrozenInstanceError):
        plan._rdf_direction = None  # type: ignore[misc]
    wrong_policy: DirectionPolicy = (
        "compound-literal" if policy == "i18n-datatype" else "i18n-datatype"
    )
    other = PreparedModels([_model()], _vocabulary(), rdf_direction=wrong_policy)
    assert not other.check_rdf(
        prepared.source, [RdfModelUsage("record", EX.root)]
    ).verified
    assert other.revision != plan.revision


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_worker_emission_and_project_model_job_forward_the_selected_policy(
    policy: DirectionPolicy, tmp_path: Path
) -> None:
    worker = WorkerExecutor(artifacts=tmp_path / "workers")
    parameters: dict[str, object] = {
        "document": _document(),
        "models": _data(),
        "usages": [{"model": "record"}],
        "rdf_direction": policy,
    }
    assert worker.run("emission", parameters).report.verified
    assert not worker.run(
        "emission", {**parameters, "rdf_direction": None}
    ).report.verified
    assert (
        worker.run(
            "emission", {**parameters, "rdf_direction": "invalid"}
        ).report.outcome
        == "input_error"
    )
    _vocabulary().serialize(tmp_path / "vocabulary.ttl", format="turtle")
    (tmp_path / "models.json").write_text(json.dumps(_data()), encoding="utf-8")
    job = {
        "name": "models",
        "kind": "models",
        "source": "terms",
        "declarations": "models.json",
        "rdf_direction": policy,
    }
    config: dict[str, object] = {
        "version": 1,
        "rdf": {"terms": {"path": "vocabulary.ttl"}},
        "checks": [job],
    }
    assert check_project(ProjectConfig(config, directory=tmp_path)).verified
    isolated = worker.run(
        "project", {"configuration": config, "directory": str(tmp_path)}
    )
    assert isolated.report.verified
    invalid = {**config, "checks": [{**job, "rdf_direction": "invalid"}]}
    assert (
        check_project(ProjectConfig(invalid, directory=tmp_path)).outcome
        == "input_error"
    )


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_serializer_corpus_keeps_policy_separate_from_actual_call_options(
    policy: DirectionPolicy,
) -> None:
    calls = []

    def serializer(value: object, *, compact: bool) -> object:
        calls.append(compact)
        return value

    case = SerializerCase(
        "arabic",
        _document(),
        serializer,
        [ModelUsage("record")],
        options={"compact": True},
    )
    result = check_serializer_corpus(
        [case],
        [_model()],
        vocabulary=_vocabulary(),
        required_branches=[BranchCoverage("record", "label", 0)],
        rdf_direction=policy,
    )
    assert result.report.verified and result.coverage.verified
    assert calls == [True]
