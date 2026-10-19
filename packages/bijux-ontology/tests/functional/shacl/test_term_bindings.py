"""Exact owned RDF term bindings survive isolated SHACL role transport."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.shacl.checking import validate_shapes

EX = Namespace("https://example.org/")


def test_literal_focus_and_owned_anonymous_shape_use_real_rdf_terms() -> None:
    root = BNode()
    shapes = prepare_source(
        Graph()
        .add((root, RDF.type, SH.NodeShape))
        .add((root, SH.datatype, XSD.integer))
    )
    shape = next(shapes.graph().subjects(RDF.type, SH.NodeShape))
    assert isinstance(shape, BNode)
    for value, conforms in (
        (Literal(4), True),
        (Literal("four", datatype=XSD.string), False),
        (Literal("01", datatype=XSD.integer, normalize=False), True),
    ):
        result = validate_shapes(
            prepare_source(Graph()),
            shapes,
            focus_nodes=(value,),
            use_shapes=(shape,),
            mode="pairs",
        )
        assert result.conforms is conforms, result.report.to_dict()
        assert result.report.verified is conforms
        if not conforms:
            assert result.findings[0].focus_node == value
    with pytest.raises(InputError, match="pairs mode"):
        validate_shapes(Graph(), shapes, focus_nodes=(Literal(4),))


def test_owned_blank_subject_is_not_skolemized_or_matched_by_foreign_label() -> None:
    blank = BNode("shared")
    data = prepare_source(Graph().add((blank, EX.value, Literal(1))))
    shape_graph = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shape_graph.add((EX.Shape, SH.nodeKind, SH.BlankNode))
    shape_graph.add((EX.Shape, SH.property, BNode("shared")))
    shape_graph.add((BNode("shared"), SH.path, EX.value))
    shape_graph.add((BNode("shared"), SH.minCount, Literal(1)))
    shapes = prepare_source(shape_graph)
    owned = next(data.graph().subjects(EX.value))
    assert isinstance(owned, BNode)
    assert validate_shapes(
        data,
        shapes,
        focus_nodes=(owned,),
        use_shapes=(EX.Shape,),
        mode="pairs",
    ).report.verified
    for foreign in (
        BNode(str(owned)),
        next(prepare_source(data.graph()).graph().subjects(EX.value)),
    ):
        assert isinstance(foreign, BNode)
        with pytest.raises(InputError, match="owned snapshot"):
            validate_shapes(
                data,
                shapes,
                focus_nodes=(foreign,),
                use_shapes=(EX.Shape,),
                mode="pairs",
            )
    with pytest.raises(InputError, match="owned snapshot"):
        validate_shapes(
            data.graph(),
            shapes,
            focus_nodes=(owned,),
            use_shapes=(EX.Shape,),
            mode="pairs",
        )


def test_anonymous_shape_keeps_nested_constraints_and_authored_identity() -> None:
    root, property_shape, metadata = BNode(), BNode(), BNode()
    graph = Graph().add((root, RDF.type, SH.NodeShape))
    graph.add((root, SH.property, property_shape))
    graph.add((property_shape, SH.path, EX.value))
    graph.add((property_shape, SH.minCount, Literal(1)))
    graph.add((metadata, EX.note, Literal("metadata")))
    shapes = prepare_source(graph)
    exported = shapes.graph()
    selected = next(exported.subjects(SH.property))
    unrelated = next(exported.subjects(EX.note))
    assert isinstance(selected, BNode) and isinstance(unrelated, BNode)
    valid_data = Graph().add((EX.item, EX.value, Literal(1)))
    valid = validate_shapes(
        valid_data, shapes, focus_nodes=(EX.item,), use_shapes=(selected,), mode="pairs"
    )
    assert valid.report.verified, valid.report.to_dict()
    invalid = validate_shapes(
        Graph(), shapes, focus_nodes=(EX.item,), use_shapes=(selected,), mode="pairs"
    )
    assert invalid.conforms is False
    assert invalid.findings[0].path is not None
    metadata_result = validate_shapes(
        Graph(), shapes, focus_nodes=(EX.item,), use_shapes=(unrelated,), mode="pairs"
    )
    assert metadata_result.report.outcome == "input_error"
    assert metadata_result.conforms is None
    with pytest.raises(InputError, match="owned snapshot"):
        validate_shapes(
            Graph(),
            shapes,
            focus_nodes=(EX.item,),
            use_shapes=(BNode(str(selected)),),
            mode="pairs",
        )


def test_equal_blank_labels_in_independent_roles_do_not_establish_a_shared_target() -> (
    None
):
    data = Graph().add((BNode("same"), EX.value, Literal(1)))
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, BNode("same")))
    shapes.add((EX.Shape, SH.property, EX.Property))
    shapes.add((EX.Property, SH.path, EX.value))
    shapes.add((EX.Property, SH.minCount, Literal(1)))
    result = validate_shapes(data, shapes)
    assert result.conforms is False
    assert not result.report.verified
