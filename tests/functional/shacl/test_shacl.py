"""Real optional backend conformance, explicit feature boundaries and isolation."""

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import RDF, SH, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.shacl import validate_shapes

EX = Namespace("https://example.org/")


def shapes_graph() -> Graph:
    shapes = Graph()
    shapes.add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetClass, EX.Record))
    shapes.add((EX.Shape, SH.property, EX.Property))
    shapes.add((EX.Property, SH.path, EX["count"]))
    shapes.add((EX.Property, SH.minCount, Literal(1)))
    shapes.add((EX.Property, SH.datatype, XSD.integer))
    return shapes


def test_raw_conformance_exact_datatype_and_rdf_details_survive_policy() -> None:
    data = Graph().add((EX.record, RDF.type, EX.Record))
    data.add((EX.record, EX["count"], Literal("1", datatype=XSD.int, normalize=False)))
    shapes = shapes_graph()
    shapes.add((EX.Property, SH.severity, SH.Warning))
    before = frozenset(data), frozenset(shapes)
    result = validate_shapes(data, shapes)
    assert result.backend.startswith("pyshacl ") and result.conforms is False
    assert result.report.issues[0].severity == "warning"
    assert not result.report.verified
    finding = result.findings[0]
    assert finding.focus_node == EX.record and finding.path == EX["count"]
    assert finding.value == Literal("1", datatype=XSD.int, normalize=False)
    assert finding.source_shape == EX.Property
    assert finding.constraint_component == SH.DatatypeConstraintComponent
    report = result.report_graph()
    assert (None, SH.conforms, Literal(False)) in report
    report.remove((None, None, None))
    assert len(result.report_graph()) > 0
    assert (frozenset(data), frozenset(shapes)) == before
    data.set(
        (EX.record, EX["count"], Literal("1", datatype=XSD.integer, normalize=False))
    )
    valid = validate_shapes(data, shapes)
    assert valid.conforms is True and valid.report.verified


def test_target_filtering_and_explicit_pairs_have_different_scope() -> None:
    data = Graph().add((EX.other, EX.name, Literal("not a Record")))
    shapes = shapes_graph()
    assert validate_shapes(data, shapes, focus_nodes=(str(EX.other),)).conforms is True
    with pytest.raises(InputError, match="bypasses targets"):
        validate_shapes(
            data, shapes, focus_nodes=(str(EX.other),), use_shapes=(str(EX.Shape),)
        )
    forced = validate_shapes(
        data,
        shapes,
        focus_nodes=(str(EX.other),),
        use_shapes=(str(EX.Shape),),
        mode="pairs",
    )
    assert forced.conforms is False and forced.mode == "pairs"
    assert forced.findings[0].focus_node == EX.other


def test_bad_shapes_unsupported_features_and_deadlines_are_not_nonconformance() -> None:
    data = Graph().add((EX.record, RDF.type, EX.Record))
    shapes = shapes_graph()
    shapes.set((EX.Property, SH.minCount, Literal("invalid")))
    invalid = validate_shapes(data, shapes, preflight=True)
    assert invalid.conforms is None and not invalid.report.complete
    assert invalid.report.issues[0].code == "shacl.shapes_invalid"
    shapes = shapes_graph().add((EX.Shape, SH.rule, BNode()))
    unsupported = validate_shapes(data, shapes)
    assert unsupported.conforms is None and not unsupported.report.complete
    assert unsupported.report.issues[0].kind == "unverified"
    bounded = validate_shapes(data, shapes_graph(), timeout=0.000001)
    assert bounded.conforms is None and not bounded.report.complete
    assert bounded.report.issues[0].kind == "exhausted"
    assert bounded.report.requested == 1 and bounded.report.checked == 0


def test_sparql_constraints_require_trust_and_reject_retrieval_before_execution() -> (
    None
):
    data = Graph().add((EX.record, RDF.type, EX.Record))
    shapes = shapes_graph()
    query = BNode()
    shapes.add((EX.Shape, SH.sparql, query))
    shapes.add(
        (
            query,
            SH.select,
            Literal(
                "SELECT $this WHERE { FILTER NOT EXISTS { "
                "$this <https://example.org/name> ?value } }"
            ),
        )
    )
    rejected = validate_shapes(data, shapes)
    assert rejected.conforms is None and not rejected.report.complete
    assert rejected.report.requested == 1 and rejected.report.checked == 0
    trusted = validate_shapes(data, shapes, trusted_sparql=True)
    assert trusted.conforms is False and trusted.report.complete
    shapes.set(
        (
            query,
            SH.select,
            Literal(
                "SELECT $this WHERE { SERVICE "
                "<https://example.org/never-fetch> { $this ?p ?o } }"
            ),
        )
    )
    with pytest.raises(InputError, match="SERVICE"):
        validate_shapes(data, shapes, trusted_sparql=True)
    shapes.set(
        (
            query,
            SH.select,
            Literal(
                "SELECT ?this FROM <https://example.org/never-fetch> "
                "WHERE { ?this ?p ?o }"
            ),
        )
    )
    with pytest.raises(InputError, match="dataset retrieval"):
        validate_shapes(data, shapes, trusted_sparql=True)


def test_blank_node_targets_and_complex_result_paths_keep_rdf_structure() -> None:
    record, property_shape, path = BNode(), BNode(), BNode()
    data = Graph().add((record, EX.marker, Literal("shared record")))
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, record))
    shapes.add((EX.Shape, SH.property, property_shape))
    shapes.add((property_shape, SH.path, path))
    shapes.add((path, SH.inversePath, EX.parent))
    shapes.add((property_shape, SH.minCount, Literal(1)))
    data.add((EX.parent_record, EX.parent, record))
    assert validate_shapes(data, shapes).conforms is True
    data.remove((EX.parent_record, EX.parent, record))
    result = validate_shapes(data, shapes)
    assert result.conforms is False
    finding = result.findings[0]
    assert isinstance(finding.focus_node, BNode) and isinstance(finding.path, BNode)
    assert (finding.path, SH.inversePath, EX.parent) in result.report_graph()
    assert isinstance(finding.source_shape, BNode)


def test_selected_parent_preserves_nested_constraints_and_excludes_other_roots() -> (
    None
):
    data = Graph().add((EX.record, RDF.type, EX.Record))
    data.add((EX.record, EX["count"], Literal("1", datatype=XSD.integer)))
    shapes = shapes_graph()
    shapes.add((EX.Other, RDF.type, SH.NodeShape))
    shapes.add((EX.Other, SH.targetNode, EX.record))
    shapes.add((EX.Other, SH.nodeKind, SH.Literal))
    before = frozenset(shapes)
    all_roots = validate_shapes(data, shapes)
    assert all_roots.conforms is False
    selected = validate_shapes(data, shapes, use_shapes=(str(EX.Shape),))
    assert selected.conforms is True
    data.remove((EX.record, EX["count"], None))
    selected = validate_shapes(data, shapes, use_shapes=(str(EX.Shape),))
    assert selected.conforms is False
    assert selected.findings[0].constraint_component == SH.MinCountConstraintComponent
    assert frozenset(shapes) == before


def test_supporting_ontology_and_imports_have_explicit_resource_roles() -> None:
    from rdflib.namespace import OWL, RDFS

    data = Graph().add((EX.record, RDF.type, EX.Child))
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, EX.record))
    shapes.add((EX.Shape, SH["class"], EX.Parent))
    ontology = Graph().add((EX.Child, RDFS.subClassOf, EX.Parent))
    assert validate_shapes(data, shapes).conforms is False
    assert validate_shapes(data, shapes, ontology=ontology).conforms is True
    shapes.add((EX.Ontology, OWL.imports, EX.unsupplied))
    with pytest.raises(InputError, match="import"):
        validate_shapes(data, shapes)


def test_empty_or_swapped_shapes_do_not_verify_an_unperformed_required_scope() -> None:
    data = Graph().add((EX.record, EX.name, Literal("Record")))
    shapes = shapes_graph()
    swapped = validate_shapes(shapes, data)
    assert swapped.conforms is True and swapped.shapes_count == 0
    assert swapped.report.outcome == "input_error" and not swapped.report.verified
    assert {issue.code for issue in swapped.report.issues} == {"input.empty_scope"}
    assert (None, SH.conforms, Literal(True)) in swapped.report_graph()
    optional = validate_shapes(data, Graph(), allow_empty=True)
    assert optional.conforms is True and optional.shapes_count == 0
    assert optional.report.outcome == "not_applicable"
    assert optional.report.exit_code() == 0 and not optional.report.verified
    # A discovered targetless shape is valid; an empty focus selection is also
    # distinct from accidentally supplying data as the shapes graph.
    targetless = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    valid = validate_shapes(data, targetless)
    assert valid.shapes_count == 1 and valid.report.verified
    filtered = validate_shapes(data, shapes, focus_nodes=(str(EX.other),))
    assert filtered.shapes_count is not None and filtered.shapes_count > 0
    assert filtered.report.verified
    implicit = Graph().add((EX.Shape, SH.targetNode, EX.record))
    implicit.add((EX.Shape, SH.property, EX.Property))
    implicit.add((EX.Property, SH.path, EX.name))
    implicit.add((EX.Property, SH.minCount, Literal(1)))
    untyped = validate_shapes(data, implicit)
    assert untyped.shapes_count == 2 and untyped.report.verified


@pytest.mark.parametrize("mode", ["targets", "pairs"])
def test_selected_metadata_cannot_verify_even_alongside_a_valid_shape(
    mode: str,
) -> None:
    from rdflib.namespace import OWL, RDFS

    data = Graph().add((EX.record, RDF.type, EX.Record))
    data.add((EX.record, EX["count"], Literal(1)))
    shapes = shapes_graph().add((EX.Metadata, RDF.type, OWL.Ontology))
    shapes.add((EX.Metadata, RDFS.label, Literal("Shapes catalogue")))
    before = frozenset(shapes)
    result = validate_shapes(
        data,
        shapes,
        use_shapes=(str(EX.Shape), str(EX.Metadata)),
        focus_nodes=(str(EX.record),) if mode == "pairs" else (),
        mode=mode,
    )
    assert result.conforms is None
    assert result.report.outcome == "input_error"
    assert not result.report.complete and not result.report.verified
    assert str(EX.Metadata) in result.report.issues[0].message
    assert frozenset(shapes) == before


def test_selected_implicit_and_empty_referenced_shapes_keep_their_constraints() -> None:
    data = Graph().add((EX.record, EX.name, Literal("Record")))
    shapes = Graph().add((EX.Shape, SH.nodeKind, SH.Literal))
    before = frozenset(shapes)
    targetless = validate_shapes(data, shapes, use_shapes=(str(EX.Shape),))
    assert targetless.report.verified and targetless.shapes_count == 1
    forced = validate_shapes(
        data,
        shapes,
        use_shapes=(str(EX.Shape),),
        focus_nodes=(str(EX.record),),
        mode="pairs",
    )
    assert forced.conforms is False
    assert forced.findings[0].constraint_component == SH.NodeKindConstraintComponent
    assert frozenset(shapes) == before
    shapes.add((EX.Shape, SH.targetNode, EX.record))
    assert validate_shapes(data, shapes, use_shapes=(str(EX.Shape),)).conforms is False
    shapes.add((EX.Shape, SH.node, EX.Empty))
    empty = validate_shapes(
        data,
        shapes,
        use_shapes=(str(EX.Empty),),
        focus_nodes=(str(EX.record),),
        mode="pairs",
    )
    assert empty.conforms is True and empty.report.verified
