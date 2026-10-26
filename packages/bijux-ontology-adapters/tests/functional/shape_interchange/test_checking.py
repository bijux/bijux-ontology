"""Actual maintained comparisons preserve one explicit common-fragment scope."""

import json
from threading import Event

import pytest
from rdflib import Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclClosed,
    ShaclCount,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shex import ShexBinding, ShexSchema
from bijux_ontology_adapters.shape_interchange import (
    check_interchange,
    shacl_to_shex,
    shex_to_shacl,
)


@pytest.fixture(autouse=True)
def _engines() -> None:
    """Native comparisons require both normally installed maintained engines."""
    pytest.importorskip("pyshacl")
    pytest.importorskip("pyrudof")


E = Namespace("urn:shape-interchange:")
_BINDINGS = (ShexBinding(E.one, E.Record),)


def _shapes(
    *, closed: bool = True, target: bool = True, class_rule: bool = False
) -> PreparedSource:
    """Author the independent consumer record through the existing typed model."""
    return compile_shapes(
        (
            ShaclNodeShape(
                E.Record,
                (ShaclShapeRef(E.Name), ShaclShapeRef(E.Age)),
                (ShaclClosed(closed),),
                target_nodes=(E.one,) if target else (),
            ),
            ShaclPropertyShape(
                E.Name, E.name, (ShaclCount(1, 1), ShaclValue(datatype=str(XSD.string)))
            ),
            ShaclPropertyShape(
                E.Age,
                E.age,
                (
                    ShaclCount(1, 1),
                    ShaclValue(class_iri=str(E.Parent))
                    if class_rule
                    else ShaclValue(datatype=str(XSD.integer)),
                ),
            ),
        )
    )


def _data(*, age: int | str = 7, extra: bool = False) -> PreparedSource:
    """Supply actual RDF records, independently of either translated schema."""
    graph = (
        Graph().add((E.one, E.name, Literal("one"))).add((E.one, E.age, Literal(age)))
    )
    if extra:
        graph.add((E.one, E.extra, Literal("additional")))
    return prepare_source(graph)


def _shex(
    *, repeated: bool = False, extra: bool = False, actions: bool = False
) -> ShexSchema:
    """Supply native standard ShExJ fields to the actual maintained backend."""
    expressions = [
        {
            "type": "TripleConstraint",
            "predicate": str(E.name),
            "valueExpr": {"type": "NodeConstraint", "datatype": str(XSD.string)},
        },
        {
            "type": "TripleConstraint",
            "predicate": str(E.age),
            "valueExpr": {"type": "NodeConstraint", "datatype": str(XSD.integer)},
        },
    ]
    if repeated:
        expressions = [expressions[1], expressions[1]]
    shape: dict[str, object] = {
        "type": "Shape",
        "closed": True,
        "expression": {"type": "EachOf", "expressions": expressions},
    }
    if extra:
        shape["extra"] = [str(E.age)]
    if actions:
        shape["semActs"] = [
            {"type": "SemAct", "name": "urn:consumer:action", "code": "required"}
        ]
    return ShexSchema(
        str(E.schema),
        json.dumps(
            {
                "@context": "http://www.w3.org/ns/shex.jsonld",
                "type": "Schema",
                "shapes": [
                    {"type": "ShapeDecl", "id": str(E.Record), "shapeExpr": shape}
                ],
            }
        ),
    )


def test_bidirectional_record_and_mutations_use_both_native_validators() -> None:
    original = _shapes()
    translated = shacl_to_shex(original, bindings=_BINDINGS, schema_iri=str(E.schema))
    assert translated.report.verified and translated.schema is not None
    assert translated.source is original and translated.inspection is not None
    assert translated.bindings == _BINDINGS and translated.preservation is not None
    assert translated.preservation.closed is True
    reverse = shex_to_shacl(translated.schema, bindings=_BINDINGS)
    assert reverse.report.verified and reverse.shapes is not None
    for plan in (translated, reverse):
        positive = check_interchange(plan, _data())
        assert positive.report.verified and positive.obligations[0].equivalent is True
        assert (
            positive.shex is not None
            and positive.shex.backend_version == "pyrudof 0.3.20"
        )
        assert positive.obligations[0].shacl is not None
        assert positive.obligations[0].shacl.conforms is True
        for mutation in (_data(age="wrong"), _data(extra=True)):
            negative = check_interchange(plan, mutation)
            assert (
                negative.report.verified and negative.obligations[0].equivalent is True
            )
            assert negative.obligations[0].shex_status == "not_matched"
            assert negative.obligations[0].shacl is not None
            assert negative.obligations[0].shacl.conforms is False
            assert negative.obligations[0].shacl.findings


def test_open_policy_and_each_focus_pair_keep_independent_truth() -> None:
    opened = shacl_to_shex(
        _shapes(closed=False), bindings=_BINDINGS, schema_iri=str(E.schema)
    )
    assert opened.report.verified and opened.preservation is not None
    assert opened.preservation.closed is False
    assert check_interchange(opened, _data(extra=True)).report.verified
    graph = _shapes().graph()
    from rdflib.namespace import SH

    graph.add((E.Record, SH.targetNode, E.two))
    bindings = (*_BINDINGS, ShexBinding(E.two, E.Record))
    plan = shacl_to_shex(
        prepare_source(graph), bindings=bindings, schema_iri=str(E.schema)
    )
    data = _data().graph()
    data.add((E.two, E.name, Literal("two"))).add((E.two, E.age, Literal("wrong")))
    checked = check_interchange(plan, prepare_source(data))
    assert checked.report.verified and len(checked.obligations) == 2
    assert tuple(item.shex_status for item in checked.obligations) == (
        "matched",
        "not_matched",
    )
    assert all(item.equivalent is True for item in checked.obligations)


def test_consumption_overlap_extra_and_semantic_actions_are_not_weakened() -> None:
    for schema in (_shex(repeated=True), _shex(extra=True), _shex(actions=True)):
        result = shex_to_shacl(schema, bindings=_BINDINGS)
        assert not result.report.verified and result.shapes is None and result.remainder
        assert result.source is schema and result.bindings == _BINDINGS
    overlap = compile_shapes(
        (
            ShaclNodeShape(
                E.Record,
                (ShaclShapeRef(E.A), ShaclShapeRef(E.B)),
                target_nodes=(E.one,),
            ),
            ShaclPropertyShape(E.A, E.age, (ShaclCount(1, 1),)),
            ShaclPropertyShape(E.B, E.age, (ShaclCount(1, 1),)),
        )
    )
    result = shacl_to_shex(overlap, bindings=_BINDINGS, schema_iri=str(E.schema))
    assert not result.report.verified and result.schema is None


def test_target_scope_and_class_closure_cannot_be_hidden() -> None:
    changed = shacl_to_shex(
        _shapes(), bindings=(ShexBinding(E.other, E.Record),), schema_iri=str(E.schema)
    )
    assert not changed.report.verified and changed.schema is None
    pairs = shacl_to_shex(
        _shapes(target=False),
        bindings=_BINDINGS,
        schema_iri=str(E.schema),
        target_policy="pairs",
    )
    assert pairs.report.verified and check_interchange(pairs, _data()).report.verified
    rejected = shacl_to_shex(
        _shapes(), bindings=_BINDINGS, schema_iri=str(E.schema), target_policy="pairs"
    )
    assert not rejected.report.verified
    class_plan = shacl_to_shex(
        _shapes(class_rule=True), bindings=_BINDINGS, schema_iri=str(E.schema)
    )
    assert not class_plan.report.verified and class_plan.schema is None
    assert class_plan.bindings[0] is _BINDINGS[0]
    # This actual subclass data is retained; it never becomes a raw exact-type test.
    graph = _data().graph()
    graph.set((E.one, E.age, E.value)).add((E.value, RDF.type, E.Child)).add(
        (E.Child, RDFS.subClassOf, E.Parent)
    )
    assert not check_interchange(class_plan, prepare_source(graph)).report.verified
    empty = shacl_to_shex(_shapes(), bindings=(), schema_iri=str(E.schema))
    assert not empty.report.verified


def test_selected_physical_graph_and_shared_bounds_remain_explicit() -> None:
    plan = shex_to_shacl(_shex(), bindings=_BINDINGS)
    dataset = Dataset()
    for triple in _data().graph():
        dataset.graph(E.selected).add(triple)
    for triple in _data(age="wrong").graph():
        dataset.graph(E.other).add(triple)
    source = prepare_source(RdfSource(dataset, graph_name=str(E.selected)))
    checked = check_interchange(plan, source)
    assert checked.report.verified and checked.data_metadata is not None
    assert checked.data_metadata.graph_name == str(E.selected)
    assert not check_interchange(plan, prepare_source(dataset)).report.verified
    for limits in (
        Limits(max_total_bytes=128),
        Limits(max_work=8),
        Limits(max_states=8),
    ):
        budget = Budget(limits)
        stopped = check_interchange(plan, _data(), budget=budget)
        assert not stopped.report.verified
        assert any(issue.kind == "exhausted" for issue in stopped.report.issues)
    cancelled = Event()
    cancelled.set()
    assert not check_interchange(plan, _data(), cancel=cancelled).report.verified
    assert check_interchange(plan, _data()).report.verified
