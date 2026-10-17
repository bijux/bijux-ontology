"""Real optional backend conformance, explicit feature boundaries and isolation."""

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, XSD

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.shacl import validate_shapes
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.coverage import ShaclCoverageContract

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
    meta_targets = shapes_graph().add((SH.NodeShape, RDFS.subClassOf, RDFS.Class))
    unsupported_target = validate_shapes(
        data, meta_targets, use_shapes=(str(EX.Shape),)
    )
    assert unsupported_target.conforms is None
    assert not unsupported_target.report.complete
    assert (
        unsupported_target.report.issues[0].code == "shacl.implicit_target_unverified"
    )
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


@pytest.mark.parametrize("prefix_mode", ("iri", "query", "declarations"))
def test_unavailable_functions_cannot_hide_violations_in_trusted_filters(
    prefix_mode: str,
) -> None:
    import pyshacl

    shapes = Graph().parse(
        data="""
@prefix ex: <https://example.org/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
ex:double a sh:SPARQLFunction ;
    sh:parameter [ sh:path ex:x ; sh:datatype xsd:integer ] ;
    sh:select "SELECT ($x * 2 AS ?result) WHERE {}" .
ex:Shape a sh:NodeShape ; sh:targetNode ex:item ; sh:sparql ex:Constraint .
ex:Safe a sh:NodeShape ; sh:targetNode ex:item ; sh:nodeKind sh:IRI .
ex:Prefixes sh:declare [ sh:prefix "ex" ;
    sh:namespace "https://example.org/"^^xsd:anyURI ] .
""",
        format="turtle",
    )
    function = "<https://example.org/double>" if prefix_mode == "iri" else "ex:double"
    prefix = "PREFIX ex: <https://example.org/> " if prefix_mode == "query" else ""
    shapes.add(
        (
            EX.Constraint,
            SH.select,
            Literal(
                prefix + "SELECT $this WHERE { $this <https://example.org/value> ?v . "
                f"FILTER({function}(?v) != 4) }}"
            ),
        )
    )
    if prefix_mode == "declarations":
        shapes.add((EX.Constraint, SH.prefixes, EX.Prefixes))
    for value in (2, 3):
        data = Graph().add((EX.item, EX.value, Literal(value)))
        result = validate_shapes(data, shapes, trusted_sparql=True)
        assert result.conforms is None and not result.report.verified
        assert any(
            issue.code == "shacl.function_unavailable"
            and issue.location == str(EX.double)
            for issue in result.report.issues
        )
        conforms, _, _ = pyshacl.validate(
            data, shacl_graph=shapes, advanced=True, js=False, inference="none"
        )
        assert conforms is (value == 2)
        assert validate_shapes(data, shapes, use_shapes=(str(EX.Safe),)).report.verified
    shapes.set(
        (
            EX.double,
            SH.select,
            Literal("SELECT (<https://example.org/missing>($x) AS ?result) WHERE {}"),
        )
    )
    transitive = validate_shapes(data, shapes, trusted_sparql=True)
    assert {
        issue.location
        for issue in transitive.report.issues
        if issue.code == "shacl.function_unavailable"
    } == {str(EX.double), str(EX.missing)}


def test_function_syntax_in_literals_and_supported_casts_remains_ordinary_sparql() -> (
    None
):
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, EX.record))
    shapes.add((EX.Shape, SH.sparql, EX.Constraint))
    shapes.add(
        (
            EX.Constraint,
            SH.select,
            Literal(
                "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
                'SELECT $this WHERE { BIND("ex:double(<https://example.org/f>)" '
                "AS ?text) "
                "FILTER(xsd:integer(2) != 2 || CONTAINS(?text, 'never')) }"
            ),
        )
    )
    assert validate_shapes(Graph(), shapes, trusted_sparql=True).report.verified


def test_required_custom_ask_component_binding_execution_and_ambiguity() -> None:
    shapes = Graph().parse(
        data="""
@prefix ex: <https://example.org/> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
ex:EqualComponent a sh:ConstraintComponent ; sh:parameter [ sh:path ex:mustEqual ] ;
    sh:validator ex:Validator .
ex:Validator a sh:SPARQLAskValidator ; sh:ask "ASK { FILTER($value = $mustEqual) }" .
ex:ValueShape a sh:PropertyShape ; sh:targetNode ex:record ; sh:path ex:value ;
    sh:minCount 1 ; ex:mustEqual 2 ; ex:description "ordinary metadata" .
""",
        format="turtle",
    )
    required = (
        ShaclConstraintRequirement(
            str(EX.ValueShape), str(EX.mustEqual), str(EX.EqualComponent)
        ),
        ShaclConstraintRequirement(
            str(EX.ValueShape), str(SH.minCount), str(SH.MinCountConstraintComponent)
        ),
    )
    for value in (2, 3):
        data = Graph().add((EX.record, EX.value, Literal(value)))
        result = validate_shapes(
            data, shapes, trusted_sparql=True, required_constraints=required
        )
        assert result.conforms is (value == 2)
        assert result.constraints is not None and result.constraints.verified
        assert result.report.verified is (value == 2)
    unknown = validate_shapes(
        data,
        shapes,
        trusted_sparql=True,
        required_constraints=(
            ShaclConstraintRequirement(str(EX.ValueShape), str(EX.description)),
        ),
    )
    assert unknown.conforms is None and unknown.report.outcome == "unverified"
    assert unknown.report.issues[0].code == "shacl.constraint_unavailable"
    shapes.remove((EX.ValueShape, SH.targetNode, None))
    targetless = validate_shapes(
        data, shapes, trusted_sparql=True, required_constraints=required
    )
    assert targetless.conforms is True and not targetless.report.verified
    assert targetless.constraints is not None
    assert targetless.constraints.issues[0].code == "shacl.constraint_not_evaluated"
    shapes.add((EX.ValueShape, SH.targetNode, EX.record))
    shapes.add((EX.ValueShape, SH.deactivated, Literal(True)))
    inactive = validate_shapes(
        data, shapes, trusted_sparql=True, required_constraints=required
    )
    assert inactive.conforms is True and inactive.constraints is not None
    assert "deactivated" in inactive.constraints.issues[0].message
    shapes.remove((EX.ValueShape, SH.deactivated, None))
    shapes.add((EX.EqualComponent, SH.validator, EX.OtherValidator))
    shapes.add((EX.OtherValidator, SH.ask, Literal("ASK { FILTER(true) }")))
    shadowed = validate_shapes(
        data, shapes, trusted_sparql=True, required_constraints=required
    )
    assert shadowed.conforms is None
    assert "shadowed" in shadowed.report.issues[0].message
    shapes.remove((EX.EqualComponent, SH.validator, EX.OtherValidator))
    shapes.remove((EX.ValueShape, EX.mustEqual, None))
    unbound = validate_shapes(
        data, shapes, trusted_sparql=True, required_constraints=required
    )
    assert unbound.conforms is None and "unbound" in unbound.report.issues[0].message
    shapes.add((EX.ValueShape, EX.mustEqual, Literal(2)))
    parameter = shapes.value(EX.EqualComponent, SH.parameter)
    shapes.remove((parameter, SH.path, None))
    malformed = validate_shapes(
        data, shapes, trusted_sparql=True, required_constraints=required
    )
    assert malformed.conforms is None
    assert malformed.report.issues[0].code == "shacl.constraint_malformed"


def test_deactivated_required_query_cannot_borrow_its_shapes_target_coverage() -> None:
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, EX.record))
    shapes.add((EX.Shape, SH.sparql, EX.Query))
    shapes.add((EX.Query, SH.select, Literal("SELECT $this WHERE {}")))
    shapes.add((EX.Query, SH.deactivated, Literal(True)))
    assert validate_shapes(Graph(), shapes, trusted_sparql=True).report.verified
    required = validate_shapes(
        Graph(),
        shapes,
        trusted_sparql=True,
        required_constraints=(
            ShaclConstraintRequirement(str(EX.Shape), str(SH.sparql)),
        ),
    )
    assert not required.report.verified
    assert "deactivated" in required.report.issues[0].message


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
    assert valid.coverage is None
    required = validate_shapes(
        data, targetless, coverage=ShaclCoverageContract(minimum_focus=1)
    )
    assert required.conforms is True and required.conformance_report.verified
    assert required.report.outcome == "disagreement"
    assert required.coverage is not None and required.coverage.targets == ()
    assert required.coverage.report.issues[0].code == "shacl.coverage_minimum"
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


def test_target_coverage_applies_filters_deactivation_and_required_pairs() -> None:
    data = Graph()
    for node in (EX.record, EX.other):
        data.add((node, RDF.type, EX.Record))
        data.add((node, EX["count"], Literal(1)))
    shapes = shapes_graph()
    contract = ShaclCoverageContract(
        minimum_focus=2,
        focus_nodes=(str(EX.record), str(EX.other)),
        pairs=((str(EX.Shape), str(EX.record)), (str(EX.Shape), str(EX.other))),
    )
    covered = validate_shapes(data, shapes, coverage=contract)
    assert covered.report.verified and covered.coverage is not None
    assert covered.coverage.report.verified
    assert {(item.shape, item.focus_node) for item in covered.coverage.targets} == {
        (EX.Shape, EX.record),
        (EX.Shape, EX.other),
    }
    shapes.add((EX.OtherShape, SH.targetNode, EX.record))
    distinct = validate_shapes(
        data, shapes, coverage=ShaclCoverageContract(minimum_focus=3)
    )
    assert distinct.conforms is True and distinct.coverage is not None
    assert len(distinct.coverage.targets) == 3
    assert distinct.coverage.report.issues[0].actual == "2"
    shapes.remove((EX.OtherShape, None, None))
    filtered = validate_shapes(
        data, shapes, focus_nodes=(str(EX.record),), coverage=contract
    )
    assert filtered.conforms is True and filtered.conformance_report.verified
    assert filtered.coverage is not None and not filtered.coverage.report.verified
    assert {issue.code for issue in filtered.coverage.report.issues} == {
        "shacl.coverage_minimum",
        "shacl.coverage_missing",
        "shacl.coverage_pair_missing",
    }
    shapes.add((EX.Shape, SH.deactivated, Literal(True)))
    deactivated = validate_shapes(data, shapes, coverage=contract)
    assert deactivated.conforms is True and not deactivated.report.verified
    assert deactivated.coverage is not None
    assert deactivated.coverage.targets == ()
    assert deactivated.coverage.deactivated_shapes == (EX.Shape,)
    shapes.remove((EX.Shape, SH.deactivated, None))
    forced = validate_shapes(
        Graph(),
        shapes,
        focus_nodes=(str(EX.forced),),
        use_shapes=(str(EX.Shape),),
        mode="pairs",
        coverage=ShaclCoverageContract(pairs=((str(EX.Shape), str(EX.forced)),)),
    )
    assert forced.conforms is False and forced.coverage is not None
    assert forced.coverage.report.verified
    assert not forced.report.verified


def test_target_coverage_respects_graph_ontology_and_incomplete_enumeration() -> None:
    data = Dataset()
    data.graph(EX.Selected).add((EX.record, RDF.type, EX.Child))
    data.graph(EX.Excluded).add((EX.outside, RDF.type, EX.Parent))
    ontology = Graph().add((EX.Child, RDFS.subClassOf, EX.Parent))
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetClass, EX.Parent))
    result = validate_shapes(
        data,
        shapes,
        graph_name=EX.Selected,
        ontology=ontology,
        coverage=ShaclCoverageContract(focus_nodes=(str(EX.record),)),
    )
    assert result.report.verified and result.coverage is not None
    assert {item.focus_node for item in result.coverage.targets} == {EX.record}
    shapes = Graph().add((EX.Shape, RDF.type, SH.NodeShape))
    shapes.add((EX.Shape, SH.targetNode, EX.record))
    shapes.add((EX.Shape, SH.targetNode, EX.other))
    exhausted = validate_shapes(
        Graph(),
        shapes,
        limits=Limits(max_states=1),
        coverage=ShaclCoverageContract(minimum_focus=2),
    )
    assert exhausted.conforms is True and exhausted.conformance_report.verified
    assert exhausted.coverage is not None
    assert exhausted.coverage.report.outcome == "exhausted"
    assert not exhausted.coverage.report.complete and not exhausted.report.verified
    assert len(exhausted.coverage.targets) == 1


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


@pytest.mark.parametrize("class_kind", (RDFS.Class, OWL.Class, EX.ShapeClass))
def test_implicit_class_targets_respect_roots_pairs_and_nested_constraints(
    class_kind: URIRef,
) -> None:
    data = Graph().add((EX.a, RDF.type, EX.A))
    data.add((EX.a, EX.name, Literal("A")))
    data.add((EX.b, RDF.type, EX.B))
    shapes = Graph()
    shapes.add((EX.ShapeClass, RDFS.subClassOf, RDFS.Class))
    for shape in (EX.A, EX.B):
        shapes.add((shape, RDF.type, SH.NodeShape))
        shapes.add((shape, RDF.type, class_kind))
    shapes.add((EX.A, SH.node, EX.B))
    shapes.add((EX.B, SH.property, EX.RequiredName))
    shapes.add((EX.RequiredName, SH.path, EX.name))
    shapes.add((EX.RequiredName, SH.minCount, Literal(1)))
    before = frozenset(shapes)
    for selected in ((), (str(EX.B),)):
        invalid = validate_shapes(data, shapes, use_shapes=selected)
        assert invalid.conforms is False and invalid.report.complete
        assert {finding.focus_node for finding in invalid.findings} == {EX.b}
    selected_a = validate_shapes(data, shapes, use_shapes=(str(EX.A),))
    assert selected_a.conforms is True and selected_a.report.verified
    data.add((EX.other_a, RDF.type, EX.A))
    paired = validate_shapes(
        data,
        shapes,
        use_shapes=(str(EX.A),),
        focus_nodes=(str(EX.a),),
        mode="pairs",
    )
    assert paired.conforms is True and paired.report.verified
    data.remove((EX.a, EX.name, None))
    for mode in ("targets", "pairs"):
        missing = validate_shapes(
            data,
            shapes,
            use_shapes=(str(EX.A),),
            focus_nodes=(str(EX.a),) if mode == "pairs" else (),
            mode=mode,
        )
        assert missing.conforms is False and missing.report.complete
        assert {finding.focus_node for finding in missing.findings} == (
            {EX.a} if mode == "pairs" else {EX.a, EX.other_a}
        )
        assert any(
            finding.constraint_component == SH.MinCountConstraintComponent
            for finding in missing.findings
        )
    assert frozenset(shapes) == before
