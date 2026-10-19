"""Logical fields preserve endpoint, intermediate and selected graph contracts."""

from dataclasses import replace

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD
from rdflib.paths import MulPath

from bijux_ontology import (
    Limits,
    OutputRequirement,
    RdfSource,
    ValueConstraints,
    ValueDeclaration,
    prepare_jsonld,
    prepare_source,
)
from bijux_ontology.declarations.path_fields import (
    IntermediateRequirement,
    PathFieldDeclaration,
)
from bijux_ontology.models.path_fields import PathFieldUsage, check_path_fields

EX = Namespace("urn:path-field:")


def _field() -> PathFieldDeclaration:
    """Declare the quantity's logical value and owned measurement explicitly."""
    return PathFieldDeclaration(
        "quantity",
        EX.hasMeasurement / EX.value,
        (ValueDeclaration("literal", datatype=str(XSD.integer)),),
        output=OutputRequirement(
            allow_null=True, allow_empty=True, minimum_values=1, maximum_values=1
        ),
        intermediates=(
            IntermediateRequirement(
                1, 1, 1, (str(EX.Measurement),), ownership="exclusive"
            ),
        ),
    )


def _data() -> Graph:
    """Produce actual records without generating them from the contract."""
    return (
        Graph()
        .add((EX.record, EX.hasMeasurement, EX.measurement))
        .add((EX.measurement, RDF.type, EX.Measurement))
        .add((EX.measurement, EX.value, Literal(3)))
    )


def test_quantity_endpoint_intermediate_and_ownership_mutations() -> None:
    """Missing inner values, wrong endpoint types and shared ownership fail locally."""
    field = _field()
    usage = (PathFieldUsage(field, EX.record),)
    assert check_path_fields(prepare_source(RdfSource(_data())), usage).verified
    changes = (
        (
            (EX.measurement, EX.value, None),
            (EX.measurement, EX.other, Literal(3)),
            "emission.value_count_mismatch",
        ),
        (
            (EX.measurement, EX.value, None),
            (EX.measurement, EX.value, Literal("3")),
            "emission.value_mismatch",
        ),
        ((EX.measurement, RDF.type, None), None, "emission.path_intermediate_mismatch"),
        (
            None,
            (EX.other, EX.hasMeasurement, EX.measurement),
            "emission.path_ownership_mismatch",
        ),
    )
    for removal, addition, expected in changes:
        data = _data()
        if removal is not None:
            data.remove(removal)
        if addition is not None:
            data.add(addition)
        result = check_path_fields(prepare_source(RdfSource(data)), usage)
        assert not result.verified and expected in {
            issue.code for issue in result.issues
        }
        assert all(issue.location == "quantity" for issue in result.issues)
    shared = _data().add((EX.other, EX.hasMeasurement, EX.measurement))
    allowed = replace(
        field, intermediates=(replace(field.intermediates[0], ownership="shared"),)
    )
    assert check_path_fields(
        prepare_source(RdfSource(shared)), (PathFieldUsage(allowed, EX.record),)
    ).verified


def test_inverse_counts_rdf_identity_and_mutable_path_ownership() -> None:
    """Inverse traversal counts exact distinct endpoints."""
    path = ~EX.owner / EX.value
    field = PathFieldDeclaration(
        "codes",
        path,
        (ValueDeclaration("literal", datatype=str(XSD.string)),),
        output=OutputRequirement(
            allow_null=True, allow_empty=True, minimum_values=1, maximum_values=1
        ),
    )
    path.args.clear()
    data = (
        Graph()
        .add((EX.child, EX.owner, EX.record))
        .add((EX.child, EX.value, Literal("same")))
        .add((EX.child, EX.value, Literal("same", datatype=XSD.string)))
    )
    assert check_path_fields(
        prepare_source(RdfSource(data)), (PathFieldUsage(field, EX.record),)
    ).verified
    data.add((EX.child, EX.value, Literal("different")))
    result = check_path_fields(
        prepare_source(RdfSource(data)), (PathFieldUsage(field, EX.record),)
    )
    assert any(issue.code == "emission.value_count_mismatch" for issue in result.issues)


def test_graph_scope_and_owned_blank_bindings_do_not_borrow_support() -> None:
    """Endpoints and descriptions stay in their selected graph and exact snapshot."""
    dataset = Dataset()
    dataset.graph(EX.G).add((EX.record, EX.hasMeasurement, EX.measurement))
    dataset.graph(EX.H).add((EX.measurement, RDF.type, EX.Measurement)).add(
        (EX.measurement, EX.value, Literal(3))
    )
    source = prepare_source(RdfSource(dataset))
    usage = (PathFieldUsage(_field(), EX.record),)
    assert not check_path_fields(source, usage, graph_name=EX.G).verified
    assert check_path_fields(source, usage, union=True).verified
    blank = BNode()
    source = prepare_source(RdfSource(Graph().add((blank, EX.value, Literal("code")))))
    owned = next(source.graph().subjects(EX.value, None))
    assert isinstance(owned, BNode)
    field = PathFieldDeclaration(
        "code",
        EX.value,
        (ValueDeclaration("literal", datatype=str(XSD.string)),),
        output=OutputRequirement(allow_null=True, allow_empty=True, minimum_values=1),
    )
    assert check_path_fields(source, (PathFieldUsage(field, owned),)).verified
    foreign = check_path_fields(source, (PathFieldUsage(field, BNode(str(owned))),))
    assert foreign.outcome == "unverified"


def test_actual_jsonld_projection_retains_conversion_and_binding_unknowns() -> None:
    """The maintained processor supplies actual RDF; source paths are never guessed."""
    pytest.importorskip("pyld")
    projected = prepare_jsonld(
        {
            "@context": {"@vocab": str(EX)},
            "@id": str(EX.record),
            "hasMeasurement": {
                "@id": str(EX.measurement),
                "@type": "Measurement",
                "value": 3,
            },
        }
    )
    assert check_path_fields(projected, (PathFieldUsage(_field(), EX.record),)).verified
    assert check_path_fields(projected, ()).outcome == "unverified"
    missing = prepare_jsonld({"@id": str(EX.record), "unmapped": 3})
    assert not check_path_fields(
        missing, (PathFieldUsage(_field(), EX.record),)
    ).verified
    dropped = prepare_jsonld(
        {
            "@context": {"value": str(EX.value)},
            "@id": str(EX.record),
            "value": "code",
            "unmappedA": 1,
            "unmappedB": 2,
            "unmappedC": 3,
        }
    )
    limited = check_path_fields(
        dropped, (PathFieldUsage(_field(), EX.record),), limits=Limits(max_issues=1)
    )
    assert limited.outcome == "exhausted"
    assert sum(issue.kind != "exhausted" for issue in limited.issues) == 1
    oversized = prepare_jsonld(
        {
            "@context": {"value": str(EX.value)},
            "@id": str(EX.record),
            "value": "code",
            "z" * 10000: 1,
        }
    )
    limited = check_path_fields(
        oversized, (PathFieldUsage(_field(), EX.record),), limits=Limits(max_bytes=1000)
    )
    assert limited.outcome == "exhausted"
    assert all(len(issue.actual or "") <= 1000 for issue in limited.issues)


def test_unsupported_paths_source_rules_and_limits_remain_required() -> None:
    """Unsupported work and bounded prefixes never turn into successful empty checks."""
    source = prepare_source(RdfSource(_data()))
    field = _field()
    repeated = replace(field, path=MulPath(EX.hasMeasurement, "+"), intermediates=())
    assert (
        check_path_fields(source, (PathFieldUsage(repeated, EX.record),)).outcome
        == "unverified"
    )
    presence = replace(field, output=replace(field.output, require_property=True))
    assert (
        check_path_fields(source, (PathFieldUsage(presence, EX.record),)).outcome
        == "unverified"
    )
    for limits in (
        Limits(max_work=1),
        Limits(max_bytes=1),
        Limits(max_states=1),
        Limits(max_resources=1),
    ):
        result = check_path_fields(
            source, (PathFieldUsage(field, EX.record),), limits=limits
        )
        assert result.outcome == "exhausted" and not result.complete


def test_typed_shacl_projection_agrees_on_supported_path_and_record_constraints() -> (
    None
):
    """Actual Core validation checks endpoint, prefix and ownership obligations."""
    pytest.importorskip("pyshacl")
    from bijux_ontology.shacl.checking import validate_shapes
    from bijux_ontology.shacl.path_fields import project_path_field

    # A finite exact enum qualifies native numeric lexical semantics as well as
    # the backend's broader datatype validation.
    field = replace(
        _field(),
        values=(
            ValueDeclaration(
                "literal",
                datatype=str(XSD.integer),
                constraints=ValueConstraints(enumeration=(Literal(3),)),
            ),
        ),
    )
    projected = project_path_field(field, (EX.record,), shape=EX.QuantityShape)
    assert projected.report.verified and projected.source is not None
    controls = [(_data(), True)]
    missing = _data()
    missing.remove((EX.measurement, EX.value, None))
    controls.append((missing, False))
    controls.append((_data().set((EX.measurement, EX.value, Literal("3"))), False))
    controls.append((_data().add((EX.other, EX.hasMeasurement, EX.measurement)), False))
    wrong_type = _data().set((EX.measurement, RDF.type, EX.Other))
    controls.append((wrong_type, False))
    for graph, expected in controls:
        native = check_path_fields(
            prepare_source(RdfSource(graph)), (PathFieldUsage(field, EX.record),)
        )
        maintained = validate_shapes(graph, projected.source, preflight=True)
        assert native.verified is expected
        assert maintained.report.verified is expected
    lexical = project_path_field(_field(), (EX.record,), shape=EX.NumericShape)
    assert lexical.source is not None and lexical.report.outcome == "unverified"
    inverse = PathFieldDeclaration(
        "inverse",
        ~EX.owner / EX.value,
        (ValueDeclaration("literal", datatype=str(XSD.string)),),
        output=OutputRequirement(
            allow_null=True, allow_empty=True, minimum_values=1, maximum_values=1
        ),
    )
    projected = project_path_field(inverse, (EX.record,), shape=EX.InverseShape)
    assert projected.report.verified and projected.source is not None
    graph = (
        Graph()
        .add((EX.child, EX.owner, EX.record))
        .add((EX.child, EX.value, Literal("one")))
    )
    assert validate_shapes(graph, projected.source).report.verified
    graph.add((EX.child, EX.value, Literal("two")))
    assert not validate_shapes(graph, projected.source).report.verified
    bounded = project_path_field(
        field, (EX.record,), shape=EX.Shape, limits=Limits(max_work=1)
    )
    assert bounded.source is None and bounded.report.outcome == "exhausted"
