"""Datatype findings survive shared consumer budgets without duplicate charges."""

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Limits
from bijux_ontology.models.checking import check_models
from bijux_ontology.models.emission import ModelUsage, check_emission
from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission
from bijux_ontology.rdf.datatype_evaluators import DatatypeEvaluator, DatatypeRegistry

EX = Namespace("https://finding-budget.example/")


def _vocabulary() -> Graph:
    """Supply only the named facts needed by the actual selected consumers."""
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.DatatypeProperty))
        .add((EX.value, RDFS.range, EX.Datatype))
        .add((EX.Datatype, RDF.type, RDFS.Datatype))
    )


def _model(*, samples: bool = True) -> ModelDeclaration:
    """Keep static sample validation distinct from actual emitted data checks."""
    value = Literal("bad", datatype=EX.Datatype, normalize=False)
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (
                    ValueDeclaration(
                        "literal", str(EX.Datatype), samples=(value,) if samples else ()
                    ),
                ),
            ),
        ),
    )


def _registry(tail: bool) -> DatatypeRegistry:
    """Supply an explicit false proof followed optionally by an unknown facet."""
    validators = (
        (lambda value: False, lambda value: None) if tail else (lambda value: False,)
    )
    return DatatypeRegistry(
        (
            DatatypeEvaluator(
                str(EX.Datatype),
                validators,
                relation=lambda required, regime: (
                    "equal" if required == str(EX.Datatype) else "unknown"
                ),
            ),
        )
    )


@pytest.mark.parametrize("cap", (1, 2))
@pytest.mark.parametrize("tail", (False, True))
def test_actual_core_consumers_retain_first_false_and_required_tail(
    cap: int, tail: bool
) -> None:
    registry, limits = _registry(tail), Limits(max_issues=cap)
    data = (
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add(
            (EX.record, EX.value, Literal("bad", datatype=EX.Datatype, normalize=False))
        )
    )
    reports = (
        check_data_usage(
            data, _vocabulary(), datatype_registry=registry, limits=limits
        ),
        check_models(
            (_model(),), _vocabulary(), datatype_registry=registry, limits=limits
        ),
        check_rdf_emission(
            prepare_source(data),
            (_model(samples=False),),
            (RdfModelUsage("record", EX.record),),
            datatype_registry=registry,
            limits=limits,
        ),
    )
    for report in reports:
        assert [issue.code for issue in report.issues].count(
            "literal.lexical_invalid"
        ) == 1
        assert not report.verified
        if tail and cap == 1:
            assert report.outcome == "exhausted"
        elif tail:
            assert "literal.datatype_unverified" in {
                issue.code for issue in report.issues
            }
            assert report.outcome == "disagreement" and not report.complete
        else:
            assert [issue.code for issue in report.issues] == [
                "literal.lexical_invalid"
            ]
    # Builtin diagnostics still spend their first retained slot in the consumer.
    builtin = Graph().add(
        (EX.record, EX.value, Literal("1e3", datatype=XSD.decimal, normalize=False))
    )
    result = check_data_usage(builtin, _vocabulary(), limits=Limits(max_issues=1))
    assert [issue.code for issue in result.issues] == ["literal.lexical_invalid"]


@pytest.mark.parametrize("cap", (1, 2))
def test_actual_jsonld_emission_retains_custom_failure_under_small_issue_budget(
    cap: int,
) -> None:
    pytest.importorskip("pyld")
    report = check_emission(
        {
            "@context": {"value": {"@id": str(EX.value), "@type": str(EX.Datatype)}},
            "@id": str(EX.record),
            "@type": str(EX.Record),
            "value": "bad",
        },
        (_model(samples=False),),
        (ModelUsage("record"),),
        datatype_registry=_registry(True),
        limits=Limits(max_issues=cap),
    )
    assert "literal.lexical_invalid" in {issue.code for issue in report.issues}
    if cap == 1:
        assert report.outcome == "exhausted"
    else:
        assert "literal.datatype_unverified" in {issue.code for issue in report.issues}
        assert not report.complete
