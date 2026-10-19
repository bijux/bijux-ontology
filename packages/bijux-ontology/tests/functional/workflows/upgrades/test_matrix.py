"""Real staged deployment checks require every selected consumer-version pair."""

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.corpus.checking import SerializerCase
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.workflows.upgrades.applications.types import (
    ApplicationOutput,
    ApplicationVersion,
)
from bijux_ontology.workflows.upgrades.matrix import (
    ConsumerVersion,
    MatrixCell,
    MatrixContext,
    check_compatibility_matrix,
)

E = Namespace("urn:deployment:")
CLIENT = ConsumerVersion("reader", "supported")
LEGACY = ConsumerVersion("reader", "legacy")


def _ontology(*, bridge: bool = False) -> Graph:
    """Keep the old property available during the actual intermediate rollout."""
    graph = Graph().add((E.Record, RDF.type, OWL.Class))
    graph.add((E.old, RDF.type, OWL.DatatypeProperty))
    if bridge:
        graph.add((E.current, RDF.type, OWL.DatatypeProperty))
    return graph


def _application(
    predicate: str,
    *,
    serializer: Callable[..., object] | None = None,
    inline_context: bool = False,
) -> ApplicationVersion:
    """Supply actual source binding and sample evidence for a consumer contract."""
    model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        (
            FieldDeclaration(
                "label",
                predicate,
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                key="label",
                emission="guaranteed",
            ),
        ),
    )
    document: dict[str, object] = {
        "@id": str(E.item),
        "@type": str(E.Record),
        "label": "alpha",
    }
    if inline_context:
        document["@context"] = {"label": predicate}
    if serializer is not None:
        return ApplicationVersion(
            models={"record": model},
            cases=(
                SerializerCase("example", "alpha", serializer, (ModelUsage("Record"),)),
            ),
        )
    return ApplicationVersion(
        models={"record": model},
        outputs={"example": ApplicationOutput(document, (ModelUsage("Record"),))},
    )


def test_actual_two_stage_rollout_and_each_supported_consumer_version() -> None:
    pytest.importorskip("pyld")

    @dataclass
    class Record:
        label: str

    calls: list[str] = []

    def emit(instance: object, *, spelling: str) -> object:
        assert isinstance(instance, Record)
        calls.append(spelling)
        return {"@id": str(E.item), "@type": str(E.Record), spelling: instance.label}

    def version(predicate: str) -> ApplicationVersion:
        source = _application(predicate)
        return ApplicationVersion(
            models=source.models,
            cases=(
                SerializerCase(
                    "example",
                    Record("alpha"),
                    emit,
                    (ModelUsage("Record"),),
                    options={"spelling": "label"},
                ),
            ),
        )

    old, current = version(str(E.old)), version(str(E.current))
    cells = (
        MatrixCell("old", "old", "old"),
        MatrixCell("old", "bridge", "old"),
        MatrixCell("current", "bridge", "current"),
        MatrixCell("current", "old", "current"),
    )
    result = check_compatibility_matrix(
        {
            "old": {CLIENT: old, LEGACY: old},
            "current": {CLIENT: current, LEGACY: current},
        },
        {"old": _ontology(), "bridge": _ontology(bridge=True)},
        {
            "old": MatrixContext({"label": str(E.old)}),
            "current": MatrixContext({"label": str(E.current)}),
        },
        cells,
        required_consumers=(CLIENT, LEGACY),
    )
    assert set(result.deployable) == set(cells[:3])
    assert not result.report.verified and result.report.outcome == "disagreement"
    assert len(result.cells) == 8 and len(calls) == 8
    untested = [row for row in result.cells if not row.selected]
    assert len(untested) == 4 and all(
        row.report is None and not row.compatible for row in untested
    )
    for row in result.cells:
        if row.selected:
            assert len(row.consumers) == 2
            for obligation in row.consumers:
                assert obligation.check is not None
                sample = obligation.check.samples["example"]
                document = sample.document
                assert isinstance(document, dict) and document["label"] == "alpha"
                assert "example" in obligation.check.sources


def test_missing_required_consumer_version_axis_or_actual_corpus_is_unknown() -> None:
    pytest.importorskip("pyld")
    version = _application(str(E.old))
    cell = MatrixCell("old", "old", "old")
    ontologies = {"old": _ontology()}
    contexts = {"old": MatrixContext({"label": str(E.old)})}
    missing = check_compatibility_matrix(
        {"old": {CLIENT: version}},
        ontologies,
        contexts,
        (cell,),
        required_consumers=(CLIENT, LEGACY),
    )
    assert not missing.report.complete and not missing.deployable
    assert missing.cells[0].consumers[0].report is not None
    assert missing.cells[0].consumers[0].report.verified
    assert missing.cells[0].consumers[1].check is None

    def unused(instance: object) -> object:
        raise AssertionError("Unselected consumer must not execute")

    unselected = check_compatibility_matrix(
        {"old": {CLIENT: version, LEGACY: _application(str(E.old), serializer=unused)}},
        ontologies,
        contexts,
        (cell,),
        required_consumers=(CLIENT,),
    )
    assert unselected.report.verified
    extra = unselected.cells[0].consumers[1]
    assert not extra.required and extra.report is None and extra.check is None
    absent = check_compatibility_matrix(
        {"old": {CLIENT: version}},
        ontologies,
        contexts,
        (MatrixCell("old", "missing", "old"),),
        required_consumers=(CLIENT,),
    )
    assert not absent.report.complete and not absent.deployable
    no_corpus = ApplicationVersion(models=version.models)
    for apps, cells, required in (
        ({"old": {CLIENT: no_corpus}}, (cell,), (CLIENT,)),
        ({"old": {CLIENT: version}}, (), (CLIENT,)),
        ({"old": {CLIENT: version}}, (cell,), ()),
    ):
        result = check_compatibility_matrix(
            apps,
            ontologies,
            contexts,
            cells,
            required_consumers=required,
        )
        assert not result.report.complete and not result.deployable


def test_context_resources_owned_and_each_selected_context_uses_its_own_loader() -> (
    None
):
    pytest.importorskip("pyld")
    url = "https://example.test/context"
    raw = {url: {"@context": {"label": str(E.old)}}}
    old = MatrixContext(url, resources=raw)
    raw[url]["@context"]["label"] = str(E.current)
    current = MatrixContext(url, resources=raw)
    contexts = {"old": old, "current": current}
    good, bad = (
        MatrixCell("old", "bridge", "old"),
        MatrixCell("old", "bridge", "current"),
    )
    result = check_compatibility_matrix(
        {"old": {CLIENT: _application(str(E.old))}},
        {"bridge": _ontology(bridge=True)},
        contexts,
        (good, bad),
        required_consumers=(CLIENT,),
    )
    assert result.deployable == (good,) and result.report.outcome == "disagreement"
    assert old.resources[url] != current.resources[url]
    assert MatrixContext().context is ABSENT and MatrixContext(None).context is None
    inline = _application(str(E.old), inline_context=True)
    explicit_null = check_compatibility_matrix(
        {"old": {CLIENT: inline}},
        {"old": _ontology()},
        {"own": MatrixContext(), "null": MatrixContext(None)},
        (MatrixCell("old", "old", "own"), MatrixCell("old", "old", "null")),
        required_consumers=(CLIENT,),
    )
    assert explicit_null.deployable == (MatrixCell("old", "old", "own"),)
    assert not explicit_null.report.verified


def test_later_budget_exhaustion_preserves_first_actual_consumer_finding() -> None:
    pytest.importorskip("pyld")
    budget = Budget()
    calls = []

    def emit(instance: object) -> object:
        calls.append(instance)
        if len(calls) == 2:
            budget.consume("work", budget.remaining("work"))
        return {
            "@id": str(E.item),
            "@type": str(E.Record),
            "label": {"@id": str(E.other)},
        }

    version = _application(str(E.old), serializer=emit)
    result = check_compatibility_matrix(
        {"old": {CLIENT: version, LEGACY: version}},
        {"old": _ontology()},
        {"old": MatrixContext({"label": str(E.old)})},
        (MatrixCell("old", "old", "old"),),
        required_consumers=(CLIENT, LEGACY),
        budget=budget,
    )
    assert len(calls) == 2
    assert result.report.outcome == "exhausted" and not result.deployable
    assert any(issue.kind == "finding" for issue in result.report.issues)
    assert result.cells[0].consumers[1].check is not None
    first = result.cells[0].consumers[0]
    assert first.report is not None and any(
        issue.kind == "finding" for issue in first.report.issues
    )


def test_inventory_bounds_duplicates_and_ontology_capture_before_callbacks() -> None:
    version = _application(str(E.old))
    cell = MatrixCell("old", "old", "old")
    args = (
        {"old": {CLIENT: version}},
        {"old": _ontology()},
        {"old": MatrixContext({"label": str(E.old)})},
    )
    for cells, required in (((cell, cell), (CLIENT,)), ((cell,), (CLIENT, CLIENT))):
        bad = check_compatibility_matrix(*args, cells, required_consumers=required)
        assert bad.report.outcome == "input_error" and not bad.deployable
    huge = MatrixCell("x" * 100000, "old", "old")
    bounded = check_compatibility_matrix(
        *args, (huge,), required_consumers=(CLIENT,), limits=Limits(max_bytes=1000)
    )
    assert bounded.report.outcome == "exhausted" and not bounded.cells
    assert all(len(issue.location) < 1000 for issue in bounded.report.issues)
    huge_consumer = ConsumerVersion("x" * 100000, "v")
    bounded_consumer = check_compatibility_matrix(
        *args,
        (cell,),
        required_consumers=(huge_consumer,),
        limits=Limits(max_bytes=1000),
    )
    assert bounded_consumer.report.outcome == "exhausted"
    assert not bounded_consumer.required_consumers
    assert bounded_consumer.requested_consumers == 1
    many = {str(index): {CLIENT: version} for index in range(20)}
    bounded_grid = check_compatibility_matrix(
        many,
        args[1],
        args[2],
        (cell,),
        required_consumers=(CLIENT,),
        limits=Limits(max_states=10),
    )
    assert bounded_grid.report.outcome == "exhausted" and not bounded_grid.deployable
    pytest.importorskip("pyld")
    second = _ontology()

    def emit(instance: object) -> object:
        second.remove((E.old, RDF.type, OWL.DatatypeProperty))
        return {"@id": str(E.item), "@type": str(E.Record), "label": "alpha"}

    actual = _application(str(E.old), serializer=emit)
    cells = (MatrixCell("old", "first", "old"), MatrixCell("old", "second", "old"))
    result = check_compatibility_matrix(
        {"old": {CLIENT: actual}},
        {"first": _ontology(), "second": second},
        args[2],
        cells,
        required_consumers=(CLIENT,),
    )
    assert result.report.verified and set(result.deployable) == set(cells)
    assert (E.old, RDF.type, OWL.DatatypeProperty) not in second
