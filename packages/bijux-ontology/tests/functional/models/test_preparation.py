"""Compiled declaration snapshots retain fresh per-record and bounded batch truth."""

from collections.abc import Iterator
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report, combine_reports
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/catalogue/")


def test_enclosing_preparation_allowance_does_not_replace_fresh_output_checks() -> None:
    allowance = Budget()
    allowance.consume("bytes", allowance.limits.max_total_bytes - 32)
    with pytest.raises(LimitExceeded):
        PreparedModels(declarations(), vocabulary(), budget=allowance)
    prepared = PreparedModels(declarations(), vocabulary(), budget=Budget())
    results = [
        prepared.check(document(invalid=invalid), (ModelUsage("node"),))
        for invalid in (False, True, False)
    ]
    assert [report.verified for report in results] == [True, False, True]


def declarations() -> tuple[ModelDeclaration, ...]:
    return (
        ModelDeclaration(
            "node",
            (str(EX.Node),),
            (
                FieldDeclaration(
                    "label",
                    str(EX.label),
                    (ValueDeclaration("literal", datatype=str(XSD.string)),),
                    emission="guaranteed",
                ),
                FieldDeclaration(
                    "child",
                    str(EX.child),
                    (ValueDeclaration("object", model="node"),),
                    emission="optional",
                ),
            ),
        ),
    )


def vocabulary() -> Graph:
    graph = Graph().add((EX.Node, RDF.type, OWL.Class))
    graph.add((EX.label, RDF.type, OWL.DatatypeProperty))
    graph.add((EX.label, RDFS.domain, EX.Node))
    graph.add((EX.label, RDFS.range, XSD.string))
    graph.add((EX.child, RDF.type, OWL.ObjectProperty))
    graph.add((EX.child, RDFS.domain, EX.Node))
    graph.add((EX.child, RDFS.range, EX.Node))
    return graph


def document(*, invalid: bool = False) -> dict[str, object]:
    return {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.root),
        "@type": "Node",
        "label": "Root",
        "child": {
            "@id": str(EX.kid),
            "@type": "Node",
            "label": 7 if invalid else "Child",
        },
    }


def baseline(
    models: tuple[ModelDeclaration, ...], graph: Graph, output: object
) -> Report:
    return combine_reports(
        "models.prepared",
        check_models(models, graph),
        check_emission(output, models, (ModelUsage("node"),)),
    )


@pytest.mark.parametrize("engine", ("source", "native"))
def test_compiled_role_checks_retain_owned_vocabulary_and_actual_type_witnesses(
    engine: str,
) -> None:
    if engine == "source":
        pytest.importorskip("pyld")
    model = ModelDeclaration(
        "node",
        (str(EX.Node),),
        (
            FieldDeclaration(
                "child",
                str(EX.child),
                (
                    ValueDeclaration(
                        "object",
                        role=ResourceRole(
                            "instance", types=(str(EX.Node),), type_mode="named"
                        ),
                    ),
                ),
            ),
        ),
    )
    ontology = (
        vocabulary()
        .add((EX.Child, RDF.type, OWL.Class))
        .add((EX.Child, RDFS.subClassOf, EX.Node))
    )
    plan = PreparedModels((model,), ontology)
    assert plan.declaration_report.verified
    ontology.remove((EX.Child, RDFS.subClassOf, EX.Node))

    def check(kind: str) -> Report:
        """Require the actual output witness against the compiled owned vocabulary."""
        if engine == "source":
            return plan.check(
                {
                    "@context": {"@vocab": str(EX)},
                    "@id": str(EX.root),
                    "@type": "Node",
                    "child": {"@id": str(EX.kid), "@type": kind},
                },
                (ModelUsage("node"),),
            )
        data = (
            Graph().add((EX.root, RDF.type, EX.Node)).add((EX.root, EX.child, EX.kid))
        )
        data.add((EX.kid, RDF.type, EX[kind]))
        return plan.check_rdf(prepare_rdf(data), (RdfModelUsage("node", EX.root),))

    assert check("Child").verified
    assert not check("Unrelated").verified


def test_batch_matches_individual_nested_recursive_and_native_contracts() -> None:
    models, ontology = declarations(), vocabulary()
    plan = PreparedModels(models, ontology)
    outputs = [document(), document(invalid=True), document()]
    reports = list(plan.check_many(iter(outputs), (ModelUsage("node"),)))
    assert [report.to_dict() for report in reports] == [
        baseline(models, ontology, output).to_dict() for output in outputs
    ]
    assert [report.verified for report in reports] == [True, False, True]
    assert any(
        issue.code == "emission.value_mismatch" and "child" in issue.location
        for issue in reports[1].issues
    )
    graph = Graph().add((EX.root, RDF.type, EX.Node))
    graph.add((EX.root, EX.label, Literal("Root")))
    graph.add((EX.root, EX.child, EX.root))
    source = prepare_rdf(graph)
    usages = (RdfModelUsage("node", EX.root),)
    native = plan.check_rdf(source, usages)
    expected = combine_reports(
        "models.prepared",
        check_models(models, ontology),
        check_rdf_emission(source, models, usages),
    )
    assert native.to_dict() == expected.to_dict() and native.verified


def test_compiled_declaration_failures_are_retained_for_every_output() -> None:
    original = declarations()[0]
    unknown = replace(
        original, fields=(replace(original.fields[0], values=(ValueDeclaration(),)),)
    )
    failed = replace(original, types=(str(EX.Missing),))
    for model in (unknown, failed):
        plan = PreparedModels((model,), vocabulary())
        assert not plan.declaration_report.verified
        reports = list(
            plan.check_many([document(), document(invalid=True)], (ModelUsage("node"),))
        )
        assert len(reports) == 2
        for report in reports:
            assert not report.verified
            assert all(
                issue in report.issues for issue in plan.declaration_report.issues
            )
        malformed = plan.check("not an object", (ModelUsage("node"),))
        assert all(
            issue in malformed.issues for issue in plan.declaration_report.issues
        )


def test_rdf_batches_share_an_allowance_without_reusing_previous_success() -> None:
    plan = PreparedModels(declarations(), vocabulary())
    graph = Graph().add((EX.root, RDF.type, EX.Node))
    graph.add((EX.root, EX.label, Literal("Root")))
    source = prepare_rdf(graph)
    usages = (RdfModelUsage("node", EX.root),)
    pilot = Budget(plan.limits)
    assert plan.check_rdf(source, usages, budget=pilot).verified
    operation = Budget(plan.limits)
    operation.consume("work", operation.limits.max_work - pilot._counts["work"])
    first = plan.check_rdf(source, usages, budget=operation)
    assert first.verified
    stopped = plan.check_rdf(source, usages, budget=operation)
    assert stopped.outcome == "exhausted" and first.verified
    assert operation._counts["work"] <= operation.limits.max_work
    graph.set((EX.root, EX.label, Literal(7)))
    assert not plan.check_rdf(prepare_rdf(graph), usages).verified
    assert plan.check_rdf(source, usages).verified
    with pytest.raises(InputError, match="conflicts"):
        plan.check_rdf(source, usages, budget=Budget(Limits(max_work=1)))


def test_owned_declarations_contexts_and_import_versions_require_a_new_plan() -> None:
    root = Graph().add((EX.ontology, OWL.imports, EX.imported))
    imported = vocabulary()
    loaded = load_vocabulary(root, imports={str(EX.imported): imported})
    models = list(declarations())
    resources: dict[str, object] = {str(EX.context): {"@context": {"@vocab": str(EX)}}}
    plan = PreparedModels(models, loaded, context=str(EX.context), resources=resources)
    output = document()
    output.pop("@context")
    assert plan.check(output, (ModelUsage("node"),)).verified
    resources[str(EX.context)] = {"@context": {"label": str(EX.changed)}}
    imported.remove((EX.label, RDFS.range, XSD.string))
    imported.add((EX.label, RDFS.range, XSD.integer))
    models[0] = replace(models[0], types=(str(EX.Missing),))
    assert plan.check(output, (ModelUsage("node"),)).verified
    changed_vocabulary = load_vocabulary(root, imports={str(EX.imported): imported})
    changed = PreparedModels(
        models, changed_vocabulary, context=str(EX.context), resources=resources
    )
    assert not changed.check(output, (ModelUsage("node"),)).verified
    assert plan.revision != changed.revision
    assert plan.resource_versions["import[0]"] != changed.resource_versions["import[0]"]
    assert (
        plan.resource_versions[f"jsonld:{EX.context}"]
        != changed.resource_versions[f"jsonld:{EX.context}"]
    )
    assert "import[0]" in plan.resource_versions
    with pytest.raises(TypeError):
        plan.resource_versions["context"] = "changed"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        plan.models = ()  # type: ignore[misc]
    with pytest.raises(InputError):
        PreparedModels(
            declarations(),
            vocabulary(),
            resources={str(EX.context): Path("context.json")},
        )


def test_new_plan_changes_actual_context_and_import_semantics_independently() -> None:
    models = declarations()
    original = vocabulary()
    plan = PreparedModels(models, original, context={"@vocab": str(EX)})
    output = document()
    output.pop("@context")
    assert plan.check(output, (ModelUsage("node"),)).verified
    context_changed = PreparedModels(
        models, original, context={"@vocab": str(EX), "label": str(EX.changed)}
    )
    assert not context_changed.check(output, (ModelUsage("node"),)).verified
    mutated = deepcopy(original)
    mutated.remove((EX.label, RDFS.range, XSD.string))
    mutated.add((EX.label, RDFS.range, XSD.integer))
    import_changed = PreparedModels(
        models,
        load_vocabulary(
            Graph().add((EX.ontology, OWL.imports, EX.imported)),
            imports={str(EX.imported): mutated},
        ),
        context={"@vocab": str(EX)},
    )
    assert not import_changed.declaration_report.verified
    assert not import_changed.check(output, (ModelUsage("node"),)).verified
    assert plan.check(output, (ModelUsage("node"),)).verified


def test_record_cap_and_cumulative_budget_report_an_unchecked_tail() -> None:
    plan = PreparedModels(declarations(), vocabulary())
    pulled: list[int] = []

    def infinite() -> Iterator[dict[str, object]]:
        while True:
            pulled.append(len(pulled))
            yield document()

    capped = list(plan.check_many(infinite(), (ModelUsage("node"),), max_records=2))
    assert len(capped) == 3 and len(pulled) == 3
    assert capped[-1].scope == "models.batch.remaining"
    assert all(report.verified for report in capped[:2])
    assert not capped[-1].complete and any(
        issue.kind == "exhausted" for issue in capped[-1].issues
    )
    finite = list(
        plan.check_many([document()] * 2, (ModelUsage("node"),), max_records=2)
    )
    assert len(finite) == 2 and all(report.verified for report in finite)
    limited = PreparedModels(
        declarations(), vocabulary(), limits=Limits(max_total_bytes=5000)
    )
    stopped = list(
        limited.check_many(infinite(), (ModelUsage("node"),), max_records=100)
    )
    assert len(stopped) < 100 and any(
        issue.kind == "exhausted" for issue in stopped[-1].issues
    )
    assert "remaining" in stopped[-1].children[-1].scope
    failed_inputs = PreparedModels(
        declarations(),
        vocabulary(),
        context={"label": str(EX.changed)},
        limits=Limits(max_issues=2),
    )
    failures = list(
        failed_inputs.check_many(infinite(), (ModelUsage("node"),), max_records=100)
    )
    assert len(failures) == 4 and failures[-1].scope == "models.batch.remaining"
    assert all(
        any(issue.kind == "input_error" for issue in report.issues)
        for report in failures[:2]
    )
    assert any(issue.kind == "exhausted" for issue in failures[-2].issues)
    with pytest.raises(InputError):
        list(plan.check_many([], (), max_records=0))
    with pytest.raises(LimitExceeded):
        PreparedModels(
            declarations(), load_vocabulary(vocabulary()), limits=Limits(max_triples=1)
        )
