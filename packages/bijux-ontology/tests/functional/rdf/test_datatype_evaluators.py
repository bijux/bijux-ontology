"""Custom datatype capabilities remain separate in actual model/data checks."""

from collections.abc import Iterator
from dataclasses import replace
from decimal import Decimal
from typing import Any, ClassVar, cast

import pytest
from pydantic import BaseModel, Field
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.serialization import check_instance
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.datatype_evaluators import DatatypeEvaluator, DatatypeRegistry
from bijux_ontology.rdf.datatype_evaluators.types import (
    Converter,
    DatatypeRelation,
    Regime,
    RelationEvaluator,
    Validator,
)
from bijux_ontology.rdf.literals.checking import check_literal, datatype_relation
from bijux_ontology.rdf.terms import term_key

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


def test_custom_facets_reach_actual_model_data_native_and_serializer_paths() -> None:
    registry = DatatypeRegistry((_evaluator(),))
    model, vocabulary = _model(), _vocabulary()
    assert check_models((model,), vocabulary, datatype_registry=registry).verified
    assert not check_models(
        (_model("9"),), vocabulary, datatype_registry=registry
    ).verified
    context = {"value": {"@id": str(EX.value), "@type": str(EX.Bounded)}}
    for text, expected in (
        ("10", True),
        ("+0012", True),
        ("20", True),
        ("9", False),
        ("21", False),
        ("12.0", False),
        (" 12 ", False),
    ):
        literal = _literal(text)
        report = check_literal(literal, datatype_registry=registry)
        assert report.verified is expected
        data = (
            Graph()
            .add((EX.record, EX.value, literal))
            .add((EX.record, RDF.type, EX.Record))
        )
        usage = check_data_usage(data, vocabulary, datatype_registry=registry)
        native = check_rdf_emission(
            prepare_source(data),
            (model,),
            (RdfModelUsage("record", EX.record),),
            datatype_registry=registry,
        )
        Record.calls.clear()
        options: dict[str, object] = {
            "mode": "json",
            "by_alias": True,
            "exclude_none": True,
        }
        result = check_instance(
            Record(kind=str(EX.Record), value=text),
            Record.model_dump,
            (model,),
            (ModelUsage("record"),),
            vocabulary=vocabulary,
            options=options,
            context=context,
            datatype_registry=registry,
        )
        assert all(
            value.verified is expected for value in (usage, native, result.report)
        )
        assert Record.calls == [options]
        assert result.output == {"@type": str(EX.Record), "value": text}
        assert str(literal) == text
        if not expected:
            assert all(
                any(issue.code == "literal.lexical_invalid" for issue in value.issues)
                for value in (usage, native, result.report)
            )
    document = {"@context": context, "@type": str(EX.Record), "value": "+0012"}
    assert check_emission(
        document, (model,), (ModelUsage("record"),), datatype_registry=registry
    ).verified
    assert not check_emission(document, (model,), (ModelUsage("record"),)).verified


def test_validity_conversion_and_directional_relation_are_independent() -> None:
    literal = _literal("+0012")
    validity_only = DatatypeRegistry(
        (replace(_evaluator(), converter=None, relation=None),)
    )
    assert check_literal(literal, datatype_registry=validity_only).verified
    assert (
        datatype_relation(
            str(EX.Bounded), str(EX.Bounded), datatype_registry=validity_only
        )
        == "unknown"
    )
    missing = validity_only.convert(literal)
    assert missing.value is ABSENT and missing.report.outcome == "unverified"
    registry = DatatypeRegistry((_evaluator(),))
    converted = registry.convert(literal)
    assert converted.report.verified and converted.value == 12
    assert converted.literal is literal and str(converted.literal) == "+0012"
    assert term_key(literal) != term_key(_literal("12"))
    assert registry.convert(_literal("12")).value == 12
    for regime in ("application", "rdf", "owl-dl"):
        assert (
            datatype_relation(
                str(EX.Bounded),
                str(XSD.integer),
                regime=regime,
                datatype_registry=registry,
            )
            == "subset"
        )
        assert (
            datatype_relation(
                str(XSD.integer),
                str(EX.Bounded),
                regime=regime,
                datatype_registry=registry,
            )
            == "unknown"
        )
    assert (
        datatype_relation(
            str(XSD.integer),
            str(XSD.double),
            regime="owl-dl",
            datatype_registry=registry,
        )
        == "disjoint"
    )
    empty = DatatypeRegistry((replace(_evaluator(), validators=()),))
    assert empty.check(literal).outcome == "unverified"
    assert empty.convert(literal).value is ABSENT
    assert registry.check(_literal("21")).outcome == "disagreement"


def test_conjunction_strict_returns_and_callback_failures_never_guess_truth() -> None:
    callback_error = type("CallbackError" * 10_000, (Exception,), {})

    def failure(value: Literal) -> bool:
        raise callback_error("private callback payload")

    for validators in ((failure, lambda value: False), (lambda value: False, failure)):
        registry = DatatypeRegistry((replace(_evaluator(), validators=validators),))
        report = registry.check(_literal("12"))
        assert report.outcome == "disagreement"
        assert any(issue.kind == "unverified" for issue in report.issues)
        assert all(
            "private callback payload" not in issue.message and len(issue.message) < 200
            for issue in report.issues
        )
    invalid = cast(Validator, lambda value: 1)
    bad_relation = cast(RelationEvaluator, lambda required, regime: True)
    registry = DatatypeRegistry(
        (DatatypeEvaluator(str(EX.Bounded), (invalid,), relation=bad_relation),)
    )
    assert registry.check(_literal("12")).outcome == "unverified"
    assert registry.relation(str(EX.Bounded), str(EX.Bounded)) == "unknown"
    failing_relation = cast(
        RelationEvaluator,
        lambda required, regime: (_ for _ in ()).throw(ValueError("private")),
    )
    assert (
        DatatypeRegistry((replace(_evaluator(), relation=failing_relation),)).relation(
            str(EX.Bounded), str(XSD.integer)
        )
        == "unknown"
    )


def test_conversion_rejects_unowned_or_unsupported_outputs_after_validity() -> None:
    invalid_results: tuple[object, ...] = ([], 1.0, None, Decimal("NaN"), "\ud800")
    for converted in invalid_results:
        converter = cast(Converter, lambda value, result=converted: result)
        registry = DatatypeRegistry((replace(_evaluator(), converter=converter),))
        result = registry.convert(_literal("12"))
        assert result.value is ABSENT and result.report.outcome == "unverified"

    def failing_conversion(value: Literal) -> int:
        raise ValueError("private conversion payload")

    failure = DatatypeRegistry(
        (replace(_evaluator(), converter=failing_conversion),)
    ).convert(_literal("12"))
    assert failure.value is ABSENT and failure.report.outcome == "unverified"
    assert all(
        "private conversion payload" not in issue.message
        for issue in failure.report.issues
    )
    calls: list[str] = []

    def convert(value: Literal) -> int:
        calls.append(str(value))
        return 12

    registry = DatatypeRegistry((replace(_evaluator(), converter=convert),))
    assert registry.convert(_literal("9")).value is ABSENT and calls == []
    assert registry.convert(_literal("12")).value == 12 and calls == ["12"]
    large = DatatypeRegistry(
        (replace(_evaluator(), converter=lambda value: "x" * 2000),),
        limits=Limits(max_bytes=1000),
    )
    assert large.convert(_literal("12")).report.outcome == "exhausted"


def test_catalog_capture_fresh_callbacks_and_public_named_identity_guards() -> None:
    class Indexed(list[DatatypeEvaluator]):
        def __iter__(self) -> Iterator[DatatypeEvaluator]:
            raise AssertionError("Use a bounded indexed catalog capture")

    state = {"valid": True}
    evaluator = DatatypeEvaluator(str(EX.Bounded), (lambda value: state["valid"],))
    supplied = Indexed([evaluator])
    registry = DatatypeRegistry(supplied)
    supplied.clear()
    assert registry.check(_literal("12")).verified
    state["valid"] = False
    assert registry.check(_literal("12")).outcome == "disagreement"
    with pytest.raises(InputError):
        DatatypeRegistry((evaluator, evaluator))
    for iri in (str(XSD.integer), str(RDF.langString), "relative"):
        with pytest.raises(InputError):
            DatatypeEvaluator(iri)
    for invalid in (BNode("urn:custom"), Literal("urn:custom")):
        with pytest.raises(InputError):
            registry.lookup(invalid)
        with pytest.raises(InputError):
            registry.relation(str(EX.Bounded), invalid)


def test_shared_limits_preserve_findings_without_retaining_unqualified_anchors() -> (
    None
):
    evaluator = DatatypeEvaluator(str(EX.Bounded), (lambda value: False,) * 20)
    registry = DatatypeRegistry((evaluator,))
    limited = registry.check(_literal("12"), budget=Budget(Limits(max_work=100)))
    assert limited.outcome == "exhausted" and not limited.complete
    assert any(issue.code == "literal.lexical_invalid" for issue in limited.issues)
    anchor = "x" * 100_000
    operation = Budget(Limits(max_bytes=1000))
    report = registry.check(_literal("12"), location=anchor, budget=operation)
    assert report.outcome == "exhausted"
    assert all(
        issue.location != anchor and len(issue.location) <= 1000
        for issue in report.issues
    )
    converted = registry.convert(
        _literal("12"), location=anchor, budget=Budget(Limits(max_bytes=1000))
    )
    assert converted.value is ABSENT and converted.report.outcome == "exhausted"
    assert all(
        issue.location != anchor and len(issue.location) <= 1000
        for issue in converted.report.issues
    )
    capped = registry.convert(_literal("12"), budget=Budget(Limits(max_issues=1)))
    assert capped.value is ABSENT and capped.report.outcome == "exhausted"
    assert any(issue.kind == "finding" for issue in capped.report.issues)
    data = (
        Graph()
        .add((EX.first, EX.value, _literal("9")))
        .add((EX.second, EX.value, _literal("12")))
    )
    actual = check_data_usage(
        data,
        _vocabulary(),
        datatype_registry=DatatypeRegistry((_evaluator(),)),
        limits=Limits(max_work=180),
    )
    assert actual.outcome == "exhausted"
    assert any(issue.code == "literal.lexical_invalid" for issue in actual.issues)


def test_custom_text_lexical_policy_is_explicit_and_budgeted_before_callbacks() -> None:
    """A supplied text capability never widens builtin or native parser limits."""
    calls: list[Literal] = []

    def text_valid(value: Literal) -> bool:
        calls.append(value)
        return value.isascii()

    term = Literal("a" * 8000, datatype=EX.Text, normalize=False)
    before = term_key(term)
    default = DatatypeRegistry((DatatypeEvaluator(str(EX.Text), (text_valid,)),))
    assert default.check(term).outcome == "exhausted" and calls == []
    for maximum in (8192, None):
        calls.clear()
        registry = DatatypeRegistry(
            (
                DatatypeEvaluator(
                    str(EX.Text),
                    (text_valid,),
                    max_lexical_chars=maximum,
                ),
            )
        )
        assert check_literal(term, datatype_registry=registry).verified
        assert calls == [term] and calls[0] is term
        calls.clear()
        for limits in (Limits(max_bytes=1024), Limits(max_work=100)):
            stopped = registry.check(term, budget=Budget(limits))
            assert stopped.outcome == "exhausted" and calls == []
        assert registry.check(term).verified and term_key(term) == before
    for invalid in (0, -1, True, "8192"):
        with pytest.raises(InputError, match="lexical limits"):
            DatatypeEvaluator(str(EX.Text), max_lexical_chars=cast(Any, invalid))
    with pytest.raises(InputError, match="standard datatype"):
        DatatypeEvaluator(str(XSD.string), (text_valid,), max_lexical_chars=None)


def test_custom_catalogue_is_shared_by_preparation_and_pydantic_samples() -> None:
    """Bound callbacks run freshly through both reusable and sampled public routes."""
    from bijux_ontology.interfaces.adapters.pydantic_serialization import (
        check_instance as check_pydantic,
    )
    from bijux_ontology.models.contracts.preparation import PreparedModels

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
