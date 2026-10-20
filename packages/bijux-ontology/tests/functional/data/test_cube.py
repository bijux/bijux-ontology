"""Actual finite Data Cube normalization, integrity and scoped coverage controls."""

from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.data.cube import CubeCheck, check_cube
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import prepare_rdf

EX = Namespace("urn:cube:")
QB = Namespace("http://purl.org/linked-data/cube#")


def cube(*, abbreviated: bool = False, measures: bool = False) -> Graph:
    """Supply a complete finite DSD; abbreviation changes only supported closure."""
    graph = Graph()
    graph.add((EX.data, RDF.type, QB.DataSet))
    graph.add((EX.data, QB.structure, EX.dsd))
    graph.add((EX.dsd, RDF.type, QB.DataStructureDefinition))
    for spec, prop, alias, kind in (
        (EX.dimension, EX.period, QB.dimension, QB.DimensionProperty),
        (EX.measure, EX.value, QB.measure, QB.MeasureProperty),
        (EX.attribute, EX.unit, QB.attribute, QB.AttributeProperty),
    ):
        graph.add((EX.dsd, QB.component, spec))
        graph.add((spec, alias if abbreviated else QB.componentProperty, prop))
        if not abbreviated:
            graph.add((prop, RDF.type, kind))
    graph.add((EX.period, RDFS.range, XSD.decimal))
    graph.add((EX.value, RDFS.range, XSD.decimal))
    graph.add((EX.attribute, QB.componentRequired, Literal(True)))
    graph.add((EX.attribute, QB.componentAttachment, QB.DataSet))
    graph.add((EX.data, EX.unit, EX.kg))
    for index, obs in enumerate((EX.o1, EX.o2), 1):
        graph.add((obs, QB.dataSet, EX.data))
        graph.add((obs, EX.period, Literal(str(index), datatype=XSD.integer)))
        graph.add((obs, EX.value, Literal(str(index * 10), datatype=XSD.decimal)))
        if not abbreviated:
            graph.add((obs, RDF.type, QB.Observation))
            graph.add((obs, EX.unit, EX.kg))
    if measures:
        graph.add((EX.dsd, QB.component, EX.type_component))
        graph.add((EX.type_component, QB.dimension, QB.measureType))
        graph.add((QB.measureType, RDFS.range, QB.MeasureProperty))
        graph.add((EX.dsd, QB.component, EX.other_component))
        graph.add((EX.other_component, QB.measure, EX.other))
        graph.set((EX.o2, EX.period, Literal("1", datatype=XSD.integer)))
        graph.add((EX.o1, QB.measureType, EX.value))
        graph.add((EX.o2, QB.measureType, EX.other))
        graph.remove((EX.o2, EX.value, None))
        graph.add((EX.o2, EX.other, Literal("20", datatype=XSD.decimal)))
    return graph


def check(graph: Graph, *, closed: tuple[str, ...] = ()) -> CubeCheck:
    """Run the public entry point on a normally prepared owned graph."""
    return check_cube(prepare_rdf(graph), complete=True, code_lists_complete=closed)


def codes(result: CubeCheck) -> set[str]:
    """Inspect actual public diagnostics without relying on message prose."""
    return {issue.code for issue in result.report.issues}


def test_normalization_coverage_and_original_ownership() -> None:
    ordinary = cube()
    abbreviated = cube(abbreviated=True)
    original = set(abbreviated)
    expected = check(ordinary)
    actual = check(abbreviated)
    assert expected.report.verified, expected.report.to_dict()
    assert actual.report.verified, actual.report.to_dict()
    assert [item.status for item in actual.coverage] == [
        item.status for item in expected.coverage
    ]
    assert {item.identifier for item in actual.coverage} == {
        f"IC-{index}" for index in range(22)
    }
    assert all(item.report is not None for item in actual.coverage)
    assert actual.normalized is not None
    assert (EX.o1, EX.unit, EX.kg) in actual.normalized.graph()
    actual.normalized.graph().remove((EX.o1, None, None))
    assert (EX.o1, EX.unit, EX.kg) in actual.normalized.graph()
    assert set(abbreviated) == original
    assert not check_cube(prepare_rdf(ordinary)).report.verified
    assert not check_cube(
        prepare_rdf(ordinary), complete=True, union=True
    ).report.verified


def test_numeric_coordinate_equality_and_located_missing_values() -> None:
    graph = cube()
    graph.set((EX.o1, EX.period, Literal("01", datatype=XSD.integer, normalize=False)))
    graph.set((EX.o2, EX.period, Literal("1.0", datatype=XSD.decimal, normalize=False)))
    result = check(graph)
    assert "cube.ic12_duplicate_coordinates" in codes(result)
    assert any(
        issue.code == "cube.ic12_duplicate_coordinates"
        and issue.location in (str(EX.o1), str(EX.o2))
        for issue in result.report.issues
    )
    graph.set((EX.o2, EX.period, Literal("1", datatype=XSD.integer, normalize=False)))
    assert "cube.ic12_duplicate_coordinates" in codes(check(graph))
    graph.remove((EX.o1, EX.period, None))
    graph.remove((EX.o2, EX.value, None))
    result = check(graph)
    assert any(
        i.code == "cube.ic11_dimension_missing" and i.location == str(EX.o1)
        for i in result.report.issues
    )
    assert any(
        i.code == "cube.ic14_measure_missing" and i.location == str(EX.o2)
        for i in result.report.issues
    )
    graph = cube()
    graph.remove((EX.period, RDFS.range, None))
    graph.add((EX.period, RDFS.range, RDF.langString))
    graph.set((EX.o1, EX.period, Literal("same", lang="en")))
    graph.set((EX.o2, EX.period, Literal("same", lang="fr")))
    assert check(graph).report.verified


def test_measure_dimension_obligations_and_measure_attachments() -> None:
    graph = cube(measures=True)
    assert check(graph).report.verified, check(graph).report.to_dict()
    graph.remove((EX.o1, EX.value, None))
    assert "cube.ic15_measure_value_missing" in codes(check(graph))
    graph.add((EX.o1, EX.value, Literal("10", datatype=XSD.decimal)))
    graph.add((EX.o1, EX.other, Literal("1", datatype=XSD.decimal)))
    assert "cube.ic16_multiple_measures" in codes(check(graph))
    graph.remove((EX.o2, None, None))
    assert "cube.ic17_measure_group_incomplete" in codes(check(graph))
    graph = cube()
    graph.set((EX.attribute, QB.componentAttachment, QB.MeasureProperty))
    for obs in (EX.o1, EX.o2):
        graph.remove((obs, EX.unit, None))
    result = check(graph)
    assert "cube.ic13_attribute_missing" in codes(result)
    assert result.normalized is not None
    assert not list(result.normalized.graph().objects(EX.o1, EX.unit))


def test_slice_normalization_and_integrity() -> None:
    graph = cube(abbreviated=True)
    graph.add((EX.data, QB.slice, EX.slice))
    graph.add((EX.slice, QB.sliceStructure, EX.key))
    graph.add((EX.key, RDF.type, QB.SliceKey))
    graph.add((EX.dsd, QB.sliceKey, EX.key))
    graph.add((EX.key, QB.componentProperty, EX.period))
    graph.add((EX.slice, QB.observation, EX.o1))
    graph.add((EX.slice, EX.period, Literal("1", datatype=XSD.integer)))
    graph.remove((EX.o1, EX.period, None))
    assert check(graph).report.verified, check(graph).report.to_dict()
    graph.remove((EX.slice, EX.period, None))
    result = check(graph)
    assert {
        "cube.ic10_slice_dimension_missing",
        "cube.ic11_dimension_missing",
    } <= codes(result)
    graph.remove((EX.o1, QB.dataSet, None))
    assert "cube.ic18_slice_dataset_conflict" in codes(check(graph))
    graph.remove((EX.dsd, QB.sliceKey, EX.key))
    graph.add((EX.key, QB.componentProperty, EX.absent))
    result = check(graph)
    assert "cube.ic7_slice_key_undeclared" in codes(result)


def coded_graph() -> Graph:
    """Replace a numeric dimension with an explicitly defined finite code list."""
    graph = cube()
    graph.set((EX.period, RDFS.range, SKOS.Concept))
    graph.add((EX.period, QB.codeList, EX.scheme))
    graph.add((EX.scheme, RDF.type, SKOS.ConceptScheme))
    for obs, term in ((EX.o1, EX.a), (EX.o2, EX.b)):
        graph.set((obs, EX.period, term))
        graph.add((term, RDF.type, SKOS.Concept))
        graph.add((term, SKOS.inScheme, EX.scheme))
    graph.add((EX.b, OWL.deprecated, Literal(True)))
    return graph


def test_closed_code_lists_open_scope_and_missing_definitions() -> None:
    graph = coded_graph()
    closed = (str(EX.scheme),)
    assert check(graph, closed=closed).report.verified, check(
        graph, closed=closed
    ).report.to_dict()
    assert not check(graph).report.verified
    graph.set((EX.o2, EX.period, EX.outside))
    assert "cube.ic19_code_not_member" in codes(check(graph, closed=closed))
    graph.remove((EX.scheme, RDF.type, None))
    assert not check(graph, closed=closed).report.verified
    graph = cube()
    graph.remove((EX.dsd, RDF.type, None))
    assert not check(graph).report.verified
    assert len(check(graph).coverage) == 22


def test_ordered_collection_and_both_hierarchy_directions() -> None:
    graph = coded_graph()
    graph.remove((EX.scheme, RDF.type, None))
    graph.add((EX.scheme, RDF.type, SKOS.Collection))
    graph.add((EX.scheme, RDF.type, SKOS.OrderedCollection))
    head, tail = BNode(), BNode()
    graph.add((EX.scheme, SKOS.memberList, head))
    graph.add((head, RDF.first, EX.a))
    graph.add((head, RDF.rest, tail))
    graph.add((tail, RDF.first, EX.b))
    graph.add((tail, RDF.rest, RDF.nil))
    result = check(graph, closed=(str(EX.scheme),))
    assert result.report.verified, result.report.to_dict()
    for inverse in (False, True):
        graph = coded_graph()
        graph.remove((EX.scheme, RDF.type, None))
        graph.add((EX.scheme, RDF.type, QB.HierarchicalCodeList))
        graph.add((EX.scheme, QB.hierarchyRoot, EX.a))
        if inverse:
            expression = BNode()
            graph.add((EX.scheme, QB.parentChildProperty, expression))
            graph.add((expression, OWL.inverseOf, EX.parent))
            graph.add((EX.b, EX.parent, EX.a))
        else:
            graph.add((EX.scheme, QB.parentChildProperty, EX.child))
            graph.add((EX.a, EX.child, EX.b))
        result = check(graph, closed=(str(EX.scheme),))
        assert result.report.verified, result.report.to_dict()
        graph.set((EX.o2, EX.period, EX.outside))
        assert (
            "cube.ic21_code_not_reachable"
            if inverse
            else "cube.ic20_code_not_reachable"
        ) in codes(check(graph, closed=(str(EX.scheme),)))


def test_physical_scope_literal_profile_and_budget_prefix() -> None:
    data = Dataset()
    for triple in cube():
        data.graph(EX.selected).add(triple)
    data.graph(EX.excluded).add((EX.bad, RDF.type, QB.Observation))
    source = prepare_rdf(data)
    result = check_cube(source, graph_name=str(EX.selected), complete=True)
    assert result.report.verified, result.report.to_dict()
    assert result.graph_name == str(EX.selected)
    graph = cube()
    graph.set((EX.o1, EX.value, Literal("bad", datatype=XSD.decimal, normalize=False)))
    assert not check(graph).report.verified
    graph = cube()
    graph.add((EX.o1, EX.unrecognized, Literal("x", datatype=EX.datatype)))
    assert not check(graph).report.verified
    graph = cube()
    graph.remove((EX.o1, EX.period, None))
    source = prepare_rdf(graph)
    pilot = Budget()
    assert "cube.ic11_dimension_missing" in codes(
        check_cube(source, complete=True, budget=pilot)
    )
    for cap in (100, pilot._counts["work"] // 2, pilot._counts["work"] - 1):
        budget = Budget(replace(Limits(), max_work=cap))
        result = check_cube(source, complete=True, budget=budget)
        assert budget._counts.get("work", 0) <= cap
        assert not result.report.verified
        retained = {id(i) for i in result.report.issues}
        assert set(budget._retained_issues) <= retained
    with pytest.raises(InputError):
        check_cube(source, limits=Limits(), budget=Budget(Limits(max_work=10)))


def test_structural_constraints_and_explicit_coverage() -> None:
    graph = cube()
    graph.add((EX.o1, QB.dataSet, EX.other_data))
    graph.add((EX.other_data, QB.structure, EX.dsd))
    assert "cube.ic1_dataset_cardinality" in codes(check(graph))
    graph = cube()
    graph.remove((EX.data, QB.structure, None))
    assert "cube.ic2_structure_cardinality" in codes(check(graph))
    graph = cube()
    graph.remove((EX.value, RDF.type, QB.MeasureProperty))
    assert "cube.ic3_measure_undeclared" in codes(check(graph))
    graph = cube()
    graph.remove((EX.period, RDFS.range, None))
    assert "cube.ic4_dimension_range_missing" in codes(check(graph))
    graph.set((EX.period, RDFS.range, SKOS.Concept))
    assert "cube.ic5_code_list_missing" in codes(check(graph))
    graph = cube()
    graph.add((EX.dimension, QB.componentRequired, Literal(False)))
    assert "cube.ic6_nonattribute_optional" in codes(check(graph))
    graph = cube()
    graph.add((EX.key, RDF.type, QB.SliceKey))
    graph.add((EX.dsd, QB.sliceKey, EX.key))
    graph.add((EX.key, QB.componentProperty, EX.absent))
    graph.add((EX.data, QB.slice, EX.slice))
    result = check(graph)
    assert {
        "cube.ic8_slice_key_inconsistent",
        "cube.ic9_slice_structure_cardinality",
    } <= codes(result)
    assert all(item.status != "not_executed" for item in result.coverage)
    graph = cube()
    graph.set((EX.o1, EX.value, Literal("text")))
    assert "cube.ic0_datatype_conflict" in codes(check(graph))
    graph = cube()
    graph.add((EX.resource, RDF.type, XSD.integer))
    result = check(graph)
    assert result.coverage[0].status == "unverified"
    assert not result.report.verified


def test_actual_slice_paths_do_not_require_extra_type_assertions() -> None:
    graph = cube()
    graph.add((EX.untyped_slice, QB.sliceStructure, EX.untyped_key))
    graph.add((EX.untyped_key, QB.componentProperty, EX.period))
    assert "cube.ic10_slice_dimension_missing" in codes(check(graph))
    graph = cube()
    graph.add((EX.untyped_data, QB.slice, EX.slice))
    graph.add((EX.slice, QB.sliceStructure, EX.key))
    graph.add((EX.slice, QB.observation, EX.o1))
    assert "cube.ic18_slice_dataset_conflict" in codes(check(graph))


def test_simultaneous_scheme_hierarchy_and_rdf_datatype_obligations() -> None:
    graph = coded_graph()
    graph.add((EX.scheme, RDF.type, QB.HierarchicalCodeList))
    graph.add((EX.scheme, QB.hierarchyRoot, EX.a))
    graph.add((EX.scheme, QB.parentChildProperty, EX.child))
    graph.add((EX.a, EX.child, EX.b))
    assert check(graph, closed=(str(EX.scheme),)).report.verified
    graph.remove((EX.b, SKOS.inScheme, EX.scheme))
    result = check(graph, closed=(str(EX.scheme),))
    assert "cube.ic19_code_not_member" in codes(result)
    assert result.coverage[20].status == "complete"
    graph = cube()
    graph.set((EX.value, RDFS.range, RDF.langString))
    assert "cube.ic0_datatype_conflict" in codes(check(graph))
    graph = cube()
    graph.add((EX.resource, RDF.type, RDF.langString))
    assert check(graph).coverage[0].status == "unverified"


def test_every_raw_component_reference_requires_qualified_coverage() -> None:
    graph = cube()
    unsupported = Literal("unread specification")
    graph.add((EX.dsd, QB.component, unsupported))
    result = check(graph)
    assert not result.report.verified
    assert any(
        issue.code == "cube.constraint_unverified"
        and issue.location == str(unsupported)
        for issue in result.report.issues
    )
    assert unsupported in {
        component.specification
        for structure in result.structures
        for component in structure.components
    }
    assert len(result.structures[0].components) == 4
    graph.remove((EX.dsd, QB.component, unsupported))
    graph.add((EX.dsd, QB.component, EX.unresolved))
    assert not check(graph).report.verified
    graph.remove((EX.dsd, QB.component, EX.unresolved))
    assert check(graph).report.verified


def test_collection_normalization_follows_only_required_membership_scope() -> None:
    graph = cube()
    graph.add((EX.unrelated, SKOS.memberList, EX.broken_head))
    result = check(graph)
    assert result.report.verified, result.report.to_dict()
    assert result.normalized is not None
    assert (EX.unrelated, SKOS.memberList, EX.broken_head) in result.normalized.graph()
    graph = coded_graph()
    graph.add((EX.scheme, SKOS.memberList, EX.scheme_ancillary_head))
    assert check(graph, closed=(str(EX.scheme),)).report.verified
    graph.remove((EX.scheme, SKOS.memberList, None))
    graph.remove((EX.scheme, RDF.type, None))
    graph.add((EX.scheme, RDF.type, SKOS.Collection))
    graph.add((EX.scheme, SKOS.member, EX.nested))
    graph.add((EX.nested, RDF.type, SKOS.OrderedCollection))
    graph.add((EX.nested, SKOS.memberList, EX.broken_head))
    result = check(graph, closed=(str(EX.scheme),))
    assert not result.report.verified
    assert any(
        issue.code == "cube.constraint_unverified" and issue.location == str(EX.nested)
        for issue in result.report.issues
    )
    graph.add((EX.broken_head, RDF.first, EX.a))
    graph.add((EX.broken_head, RDF.rest, EX.tail))
    graph.add((EX.tail, RDF.first, EX.b))
    graph.add((EX.tail, RDF.rest, RDF.nil))
    graph.add((EX.unrelated, SKOS.memberList, EX.other_broken_head))
    result = check(graph, closed=(str(EX.scheme),))
    assert result.report.verified, result.report.to_dict()
    source = prepare_rdf(graph)
    for cap in (1, len(graph), 2 * len(graph)):
        budget = Budget(Limits(max_triples=cap))
        bounded = check_cube(
            source, complete=True, code_lists_complete=(str(EX.scheme),), budget=budget
        )
        assert bounded.report.outcome in ("agreement", "exhausted")
        assert budget._counts.get("triples", 0) <= cap
