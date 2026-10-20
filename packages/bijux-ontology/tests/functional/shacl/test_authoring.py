"""Typed consumer-authored constraints compile to ordinary checked SHACL RDF."""

from collections.abc import Iterator
from typing import cast

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.paths import SequencePath

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.paths.types import RdfPath, capture_path
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclClosed,
    ShaclComparison,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclQualifiedCount,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.inspection.reading import inspect_shapes
from bijux_ontology.shacl.paths import ShaclPath, decode_path

EX = Namespace("https://example.invalid/catalogue/")


def test_closed_policy_and_ignored_predicates_survive_typed_inspection() -> None:
    """Closed true/false and ignored predicates keep actual maintained semantics."""
    pytest.importorskip("pyshacl")
    for policy in (
        ShaclClosed(True),
        ShaclClosed(True, (str(RDF.type),)),
        ShaclClosed(False),
    ):
        source = compile_shapes(
            (
                ShaclNodeShape(
                    EX.RecordShape,
                    properties=(ShaclShapeRef(EX.NameShape),),
                    constraints=(policy,),
                ),
                ShaclPropertyShape(
                    EX.NameShape,
                    EX.name,
                    (ShaclCount(1, 1), ShaclValue(datatype=str(XSD.string))),
                ),
            )
        )
        inspection = inspect_shapes(source, (EX.RecordShape,))
        assert inspection.report.verified and not inspection.remainder
        root = next(
            item for item in inspection.definitions if item.identifier == EX.RecordShape
        )
        assert policy in root.constraints
        rebuilt = compile_shapes(inspection.definitions)
        data = Graph()
        data.add((EX.record, EX.name, Literal("name")))
        ordinary = validate_shapes(
            data,
            rebuilt,
            focus_nodes=(EX.record,),
            use_shapes=(EX.RecordShape,),
            mode="pairs",
            preflight=True,
        )
        assert ordinary.report.verified and ordinary.conforms is True
        data.add((EX.record, RDF.type, EX.Record))
        typed = validate_shapes(
            data,
            rebuilt,
            focus_nodes=(EX.record,),
            use_shapes=(EX.RecordShape,),
            mode="pairs",
        )
        assert typed.report.complete and typed.conforms is (
            not policy.enabled or bool(policy.ignored_properties)
        )
        data.add((EX.record, EX.unexpected, Literal("extra")))
        extra = validate_shapes(
            data,
            rebuilt,
            focus_nodes=(EX.record,),
            use_shapes=(EX.RecordShape,),
            mode="pairs",
        )
        assert extra.report.complete and extra.conforms is (not policy.enabled)
    broken = source.graph()
    head = BNode()
    broken.add((EX.RecordShape, SH.ignoredProperties, head))
    broken.add((head, RDF.first, Literal("not a predicate")))
    broken.add((head, RDF.rest, RDF.nil))
    from bijux_ontology.rdf.sources.loading import prepare_rdf

    assert not inspect_shapes(prepare_rdf(broken), (EX.RecordShape,)).report.verified


def _consumer_shapes() -> list[ShaclNodeShape | ShaclPropertyShape]:
    """Author a catalogue submission contract independently of its actual records."""
    return [
        ShaclNodeShape(
            EX.BatchShape,
            properties=tuple(
                ShaclShapeRef(iri) for iri in (EX.Start, EX.End, EX.Members, EX.Code)
            ),
            target_classes=(str(EX.Batch),),
        ),
        ShaclPropertyShape(
            EX.Start,
            EX.start,
            (ShaclCount(1, 1), ShaclComparison("less_than_or_equals", str(EX.end))),
        ),
        ShaclPropertyShape(EX.End, EX.end, (ShaclCount(1, 1),)),
        ShaclPropertyShape(
            EX.Members,
            EX.member,
            (ShaclQualifiedCount(ShaclShapeRef(EX.ActiveShape), 1, 1),),
        ),
        ShaclPropertyShape(
            EX.Code,
            EX.code,
            (
                ShaclCount(1, 1),
                ShaclLogical(
                    "or", (ShaclShapeRef(EX.StringShape), ShaclShapeRef(EX.IriShape))
                ),
            ),
        ),
        ShaclNodeShape(
            EX.ActiveShape, constraints=(ShaclValue(class_iri=str(EX.Active)),)
        ),
        ShaclNodeShape(
            EX.StringShape, constraints=(ShaclValue(datatype=str(XSD.string)),)
        ),
        ShaclNodeShape(EX.IriShape, constraints=(ShaclValue(node_kind="iri"),)),
    ]


def _consumer_data() -> Graph:
    """Produce actual data without consulting or generating from the shape records."""
    return Graph().parse(
        data=f"""
        @prefix ex: <{EX}> .
        ex:batch a ex:Batch ; ex:start 1 ; ex:end 2 ;
          ex:member ex:active, ex:inactive ; ex:code "catalogue" .
        ex:active a ex:Active .
    """,
        format="turtle",
    )


def test_independent_consumer_meta_shacl_and_actual_mutations() -> None:
    """Cross-property, qualified and logical rules are real backend obligations."""
    pytest.importorskip("pyshacl")
    prepared = compile_shapes(_consumer_shapes())
    # A serialized ordinary export is accepted independently by the backend.
    exported = Graph().parse(
        data=prepared.graph().serialize(format="turtle"), format="turtle"
    )
    result = validate_shapes(_consumer_data(), exported, preflight=True)
    assert result.conforms is True and result.report.verified
    controls = (
        (SH.LessThanOrEqualsConstraintComponent, EX.batch, EX.start, Literal(3)),
        (SH.QualifiedMinCountConstraintComponent, EX.active, RDF.type, None),
        (SH.OrConstraintComponent, EX.batch, EX.code, Literal(7)),
    )
    for component, subject, predicate, replacement in controls:
        data = _consumer_data()
        data.remove((subject, predicate, None))
        if replacement is not None:
            data.add((subject, predicate, replacement))
        checked = validate_shapes(data, exported, preflight=True)
        assert checked.conforms is False and checked.findings
        assert component in {item.constraint_component for item in checked.findings}


def test_reused_complex_path_and_recursive_references_remain_rdf_links() -> None:
    """Paths share one representation and recursion never expands Python definitions."""
    pytest.importorskip("pyshacl")
    shared = capture_path(~EX.owner / EX.name)
    prepared = compile_shapes(
        [
            ShaclNodeShape(
                EX.Shape,
                (ShaclShapeRef(EX.P), ShaclShapeRef(EX.Q)),
                target_nodes=(EX.record,),
            ),
            ShaclPropertyShape(EX.P, shared, (ShaclCount(1),)),
            ShaclPropertyShape(EX.Q, shared, (ShaclValue(datatype=str(XSD.string)),)),
        ]
    )
    graph = prepared.graph()
    for root in (EX.P, EX.Q):
        node = graph.value(root, SH.path)
        assert node is not None
        path = decode_path(graph, node)
        assert path.kind == "sequence" and path.children[0].kind == "inverse"
    data = Graph().parse(
        data=f"@prefix ex:<{EX}>. ex:parent ex:owner ex:record; ex:name 'catalogue'.",
        format="turtle",
    )
    assert validate_shapes(data, prepared, preflight=True).report.verified
    data.remove((EX.parent, EX.name, None))
    assert validate_shapes(data, prepared).conforms is False
    recursive = compile_shapes(
        [
            ShaclNodeShape(EX.Recursive, (ShaclShapeRef(EX.Child),)),
            ShaclPropertyShape(EX.Child, EX.child, (ShaclShapeRef(EX.Recursive),)),
        ]
    ).graph()
    assert (EX.Child, SH.node, EX.Recursive) in recursive and len(recursive) == 5


def test_shared_path_capture_owns_mutable_inputs_and_rejects_invalid_structure() -> (
    None
):
    """The shared path boundary snapshots maintained objects and rejects uncertainty."""
    mutable = EX.a / ~EX.b
    path = capture_path(mutable)
    declared = ShaclPropertyShape(EX.P, mutable)
    mutable.args.clear()
    assert ShaclPath is RdfPath and len(path.children) == 2
    graph = compile_shapes([declared]).graph()
    node = graph.value(EX.P, SH.path)
    assert node is not None and decode_path(graph, node).readable == path.readable
    for raw in (
        URIRef("relative"),
        SequencePath(EX.a),
        -EX.a,
        RdfPath("inverse", BNode(), ()),
    ):
        with pytest.raises(InputError):
            capture_path(raw)
    cycle = EX.a / EX.b
    cycle.args.append(cycle)
    with pytest.raises(InputError):
        capture_path(cycle)
    with pytest.raises(LimitExceeded):
        capture_path(path, limits=Limits(max_states=1))
    with pytest.raises(LimitExceeded):
        capture_path(URIRef("urn:" + "x" * 1_000_000), limits=Limits(max_work=1))


def test_compile_fails_closed_and_preserves_exact_owned_constants() -> None:
    """Unsupported inputs and conflicting parameters return no partial artifact."""
    invalid: list[list[ShaclNodeShape | ShaclPropertyShape]] = [
        [ShaclNodeShape(EX.S, constraints=(ShaclShapeRef(EX.Missing),))],
        [ShaclNodeShape(EX.S), ShaclNodeShape(EX.S)],
        [ShaclNodeShape(EX.S, constraints=(ShaclCount(1),))],
        [ShaclPropertyShape(EX.P, EX.a, (ShaclCount(True),))],
        [
            ShaclPropertyShape(
                EX.P,
                EX.a,
                (
                    ShaclValue(datatype=str(XSD.string)),
                    ShaclValue(datatype=str(XSD.integer)),
                ),
            )
        ],
        [ShaclNodeShape(EX.S, constraints=(cast("ShaclValue", object()),))],
    ]
    for definitions in invalid:
        with pytest.raises(InputError):
            compile_shapes(definitions)
    original = Literal("01", datatype=XSD.integer, normalize=False)
    prepared = compile_shapes(
        [
            ShaclPropertyShape(
                EX.P, EX.a, (ShaclValue(has_value=original, in_values=()),)
            )
        ]
    )
    original._datatype = XSD.string
    exported = prepared.graph()
    retained = exported.value(EX.P, SH.hasValue)
    assert (
        isinstance(retained, Literal)
        and str(retained) == "01"
        and retained.datatype == XSD.integer
    )
    assert exported.value(EX.P, SH["in"]) == RDF.nil
    exported.remove((None, None, None))
    assert len(prepared.graph()) == 4


def test_bounded_capture_and_metadata_never_return_partial_shapes() -> None:
    """Bound indexed capture and compilation with cumulative operation caps."""

    class Definitions(list[ShaclNodeShape]):
        """Keep normal finite indexing while forbidding arbitrary iteration."""

        def __iter__(self) -> Iterator[ShaclNodeShape]:
            raise AssertionError("compiler must not trust a caller iterator")

    assert len(compile_shapes(Definitions([ShaclNodeShape(EX.S)])).graph()) == 1
    for limits in (
        Limits(max_states=1),
        Limits(max_work=1),
        Limits(max_bytes=1),
        Limits(max_triples=1),
    ):
        with pytest.raises(LimitExceeded):
            compile_shapes(_consumer_shapes(), limits=limits)
