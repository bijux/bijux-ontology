"""Framework extraction retains the ordinary core semantic contracts."""

from dataclasses import replace
from typing import Any, ClassVar

from pydantic import BaseModel, Field
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.datatype_evaluators import DatatypeEvaluator, DatatypeRegistry
from bijux_ontology.rdf.datatype_evaluators.types import (
    DatatypeRelation,
    Regime,
)
from bijux_ontology.rdf.literals.checking import check_literal

EX = Namespace("https://example.org/custom-datatype/")


def _integer(value: Literal) -> bool:
    """Reuse the existing raw XSD lexical check before any numeric conversion."""
    return check_literal(
        Literal(str(value), datatype=XSD.integer, normalize=False)
    ).verified


def _minimum(value: Literal) -> bool | None:
    """One consumer-supplied value facet; lexical qualification is independent."""
    try:
        return int(str(value)) >= 10
    except ValueError:
        return None


def _maximum(value: Literal) -> bool | None:
    """Keep conjunction explicit rather than introducing a facet language."""
    try:
        return int(str(value)) <= 20
    except ValueError:
        return None


def _relation(required: str, regime: Regime) -> DatatypeRelation:
    """Supply exactly the two directional claims supported by this definition."""
    if required == str(EX.Bounded):
        return "equal"
    return "subset" if required == str(XSD.integer) else "unknown"


def _evaluator() -> DatatypeEvaluator:
    return DatatypeEvaluator(
        str(EX.Bounded),
        (_integer, _minimum, _maximum),
        converter=lambda value: int(str(value)),
        relation=_relation,
    )


def _literal(value: str) -> Literal:
    return Literal(value, datatype=EX.Bounded, normalize=False)


def _model(sample: str = "+0012") -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (
                    ValueDeclaration(
                        "literal", str(EX.Bounded), samples=(_literal(sample),)
                    ),
                ),
            ),
        ),
    )


def _vocabulary() -> Graph:
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.DatatypeProperty))
        .add((EX.value, RDFS.range, XSD.integer))
        .add((EX.Bounded, RDF.type, RDFS.Datatype))
    )


class Record(BaseModel):
    calls: ClassVar[list[dict[str, object]]] = []
    kind: str = Field(serialization_alias="@type")
    value: str

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        type(self).calls.append(kwargs)
        return super().model_dump(**kwargs)


def test_custom_catalogue_is_shared_by_preparation_and_pydantic_samples() -> None:
    """Bound callbacks run freshly through both reusable and sampled public routes."""
    from bijux_ontology.models.contracts.preparation import PreparedModels
    from bijux_ontology_adapters.pydantic_serialization import (
        check_instance as check_pydantic,
    )

    allow = True
    seen = []

    def validity(value: Literal) -> bool:
        seen.append(str(value))
        return (
            allow
            and _integer(value)
            and _minimum(value) is True
            and _maximum(value) is True
        )

    evaluator = replace(_evaluator(), validators=(validity,))
    supplied = [evaluator]
    registry = DatatypeRegistry(supplied)
    model, vocabulary = _model(), _vocabulary()
    context = {"value": {"@id": str(EX.value), "@type": str(EX.Bounded)}}
    document = {"@type": str(EX.Record), "value": "+0012"}
    plan = PreparedModels(
        (model,), vocabulary, context=context, datatype_registry=registry
    )
    supplied.clear()
    assert plan.declaration_report.verified
    assert plan.check(document, (ModelUsage("record"),)).verified
    data = (
        Graph()
        .add((EX.record, EX.value, _literal("+0012")))
        .add((EX.record, RDF.type, EX.Record))
    )
    assert plan.check_rdf(
        prepare_source(data), (RdfModelUsage("record", EX.record),)
    ).verified
    assert check_pydantic(
        Record(kind=str(EX.Record), value="+0012"),
        vocabulary=vocabulary,
        options={"mode": "json", "by_alias": True},
        context=context,
        inventory=(model,),
        model="record",
        datatype_registry=registry,
    ).report.verified
    from bijux_ontology.models.persistence.loading import (
        compile_model_artifact,
        load_model_artifact,
    )
    from bijux_ontology.rdf.sources.types import RdfSource
    from bijux_ontology.workflows.revisions.preparation import prepare_revision

    source = RdfSource(vocabulary.serialize(format="turtle"), format="turtle")
    artifact = compile_model_artifact(
        (model,), source, context=context, datatype_registry=registry
    )
    assert artifact.report.verified and artifact.content is not None
    assert artifact.identity is not None and artifact.sha256 is not None
    restored = load_model_artifact(
        artifact.content,
        expected=artifact.identity,
        trusted_sha256=artifact.sha256,
        datatype_registry=registry,
    )
    assert restored.report.verified and restored.prepared is not None
    assert restored.prepared.check(document, (ModelUsage("record"),)).verified
    missing_binding = load_model_artifact(
        artifact.content,
        expected=artifact.identity,
        trusted_sha256=artifact.sha256,
    )
    assert not missing_binding.report.verified
    revision = prepare_revision(
        (model,), source, context=context, datatype_registry=registry
    )
    assert revision.report.verified and revision.prepared is not None
    revision.prepared.close()
    before = len(seen)
    allow = False
    rejected = plan.check(document, (ModelUsage("record"),))
    assert len(seen) > before and not rejected.verified
    assert any(issue.code == "literal.lexical_invalid" for issue in rejected)
    missing = PreparedModels((model,), vocabulary, context=context)
    assert not missing.declaration_report.verified
    assert not missing.check(document, (ModelUsage("record"),)).verified
