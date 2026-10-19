"""Generated ordinary SHACL preserves explicitly authored output obligations."""

import importlib.util
from dataclasses import replace
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, RDFS, SH, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.model_shapes import ModelShapeBinding, compile_model_shapes

EX = Namespace("https://example.invalid/output/")


def _binding() -> ModelShapeBinding:
    """Select one actual record independently of its declared types."""
    return ModelShapeBinding("Record", EX.RecordShape, focus_nodes=(EX.record,))


def test_exact_types_datatypes_enum_counts_and_no_implicit_required_fields() -> None:
    """A subclass edge and numeric datatype compatibility cannot weaken exact rules."""
    pytest.importorskip("pyshacl")
    one = literal_term("01", datatype=XSD.integer)
    model = ModelDeclaration(
        "Record",
        (str(EX.Super), str(EX.Tag)),
        (
            FieldDeclaration(
                "code",
                str(EX.code),
                (
                    ValueDeclaration(
                        "literal",
                        str(XSD.integer),
                        constraints=ValueConstraints(enumeration=(one,)),
                    ),
                ),
                emission="guaranteed",
                output=OutputRequirement(
                    allow_null=True,
                    allow_empty=True,
                    minimum_values=1,
                    maximum_values=1,
                ),
            ),
            FieldDeclaration("unused", str(EX.unused), (ValueDeclaration("iri"),)),
        ),
    )
    translated = compile_model_shapes([model], [_binding()])
    assert translated.source is not None and translated.report.verified
    shapes = translated.source.graph()
    assert not list(shapes.triples((None, SH["class"], None)))
    assert set(shapes.objects(None, SH.hasValue)) == {EX.Super, EX.Tag}
    data = Graph()
    data.add((EX.record, RDF.type, EX.Super))
    data.add((EX.record, RDF.type, EX.Tag))
    data.add((EX.record, EX.code, one))
    data.add((EX.Sub, RDFS.subClassOf, EX.Super))
    iri_metadata = replace(
        model,
        types=(EX.Super, EX.Tag),
        fields=(
            replace(
                model.fields[0],
                predicate=EX.code,
                values=(replace(model.fields[0].values[0], datatype=XSD.integer),),
            ),
            model.fields[1],
        ),
        subject=SubjectIdentity(expected_iri=EX.record),
    )
    iri_shapes = compile_model_shapes([iri_metadata], [_binding()])
    assert iri_shapes.report.verified and iri_shapes.source is not None
    assert check_rdf_emission(
        prepare_rdf(data), [iri_metadata], [RdfModelUsage("Record", EX.record)]
    ).verified
    assert validate_shapes(data, iri_shapes.source, preflight=True).conforms is True
    for mutation in (None, "type", "datatype", "enum", "missing", "count"):
        actual = Graph()
        for triple in data:
            actual.add(triple)
        if mutation == "type":
            actual.remove((EX.record, RDF.type, EX.Super))
            actual.add((EX.record, RDF.type, EX.Sub))
        elif mutation in ("datatype", "enum"):
            actual.remove((EX.record, EX.code, None))
            actual.add(
                (
                    EX.record,
                    EX.code,
                    literal_term(
                        "01" if mutation == "datatype" else "1",
                        datatype=XSD.int if mutation == "datatype" else XSD.integer,
                    ),
                )
            )
        elif mutation == "missing":
            actual.remove((EX.record, EX.code, None))
        elif mutation == "count":
            actual.add((EX.record, EX.code, literal_term("1", datatype=XSD.integer)))
        native = check_rdf_emission(
            prepare_rdf(actual), [model], [RdfModelUsage("Record", EX.record)]
        )
        backend = validate_shapes(actual, translated.source, preflight=True)
        assert native.verified == (mutation is None)
        assert backend.conforms == (mutation is None)


def test_inverse_alternatives_simultaneous_types_and_untargeted_children() -> None:
    """Reusable child definitions impose real constraints only on referenced values."""
    pytest.importorskip("pyshacl")
    models = [
        ModelDeclaration(
            "Record",
            (str(EX.Record),),
            (
                FieldDeclaration(
                    "owner",
                    str(EX.owns),
                    (
                        ValueDeclaration(
                            "object", types=(str(EX.Owner), str(EX.Active))
                        ),
                        ValueDeclaration("iri", types=(str(EX.Service),)),
                    ),
                    reverse=True,
                    emission="guaranteed",
                ),
                FieldDeclaration(
                    "child",
                    str(EX.child),
                    (ValueDeclaration("object", model="Child"),),
                    emission="guaranteed",
                ),
            ),
        ),
        ModelDeclaration(
            "Child",
            (str(EX.Child),),
            (
                FieldDeclaration(
                    "name",
                    str(EX.name),
                    (ValueDeclaration("literal", str(XSD.string)),),
                    emission="guaranteed",
                ),
            ),
        ),
    ]
    translated = compile_model_shapes(
        models, [_binding(), ModelShapeBinding("Child", EX.ChildShape)]
    )
    assert translated.source is not None and translated.report.verified
    data = Graph().parse(
        data=f"""@prefix ex: <{EX}> .
        ex:record a ex:Record; ex:child ex:child1.
        ex:owner a ex:Owner, ex:Active; ex:owns ex:record.
        ex:child1 a ex:Child; ex:name "child".""",
        format="turtle",
    )
    for mutation in (None, "inverse", "alternative", "child"):
        actual = Graph()
        for triple in data:
            actual.add(triple)
        if mutation == "inverse":
            actual.remove((EX.owner, EX.owns, EX.record))
            actual.add((EX.record, EX.owns, EX.owner))
        elif mutation == "alternative":
            actual.remove((EX.owner, RDF.type, EX.Active))
        elif mutation == "child":
            actual.remove((EX.child1, EX.name, None))
        native = check_rdf_emission(
            prepare_rdf(actual), models, [RdfModelUsage("Record", EX.record)]
        )
        checked = validate_shapes(actual, translated.source, preflight=True)
        assert native.verified == (mutation is None)
        assert checked.conforms == (mutation is None)


def test_partial_translation_retains_conditions_source_rules_and_selection_report() -> (
    None
):
    """A usable partial source cannot conceal a required source or selection gap."""
    selection = Report(
        "consumer.selected_terms", checked=1, requested=2, complete=False
    )
    enumeration = ValueEnumeration((EX.good,), report=selection)
    model = ModelDeclaration(
        "Record",
        (),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("literal", str(XSD.integer)),),
                output=OutputRequirement(require_property=True),
            ),
            FieldDeclaration(
                "choice",
                str(EX.choice),
                (
                    ValueDeclaration(
                        "iri", constraints=ValueConstraints(enumeration=enumeration)
                    ),
                ),
            ),
        ),
        emission_groups=(
            EmissionGroup(
                "conditional",
                ("value", "choice"),
                scenarios=(
                    EmissionScenario(
                        "selected", ("choice",), EmissionCondition("value", Literal(1))
                    ),
                ),
            ),
        ),
    )
    result = compile_model_shapes([model], [_binding()])
    assert result.source is not None and not result.report.verified
    assert not result.report.complete
    assert result.report.children == (selection,)
    assert len(result.report.issues) >= 3
    assert all(issue.kind == "unverified" for issue in result.report.issues)
    assert (None, SH.datatype, XSD.integer) in result.source.graph()
    assert not list(result.source.graph().triples((None, SH["in"], None)))
    if pytest.importorskip("pyshacl"):
        data = Graph()
        data.add((EX.record, EX.value, literal_term(" 1", datatype=XSD.integer)))
        backend = validate_shapes(data, result.source)
        assert backend.conforms is True
        assert not check_rdf_emission(
            prepare_rdf(data), [model], [RdfModelUsage("Record", EX.record)]
        ).verified


def test_owned_enum_terms_required_prefix_and_metadata_bounds() -> None:
    """Compiled definitions own constants and exhaustion preserves prior obligations."""
    term = literal_term("original", datatype=XSD.string)
    model = ModelDeclaration(
        "Record",
        (),
        (
            FieldDeclaration(
                "name",
                str(EX.name),
                (
                    ValueDeclaration(
                        "literal",
                        str(XSD.string),
                        constraints=ValueConstraints(enumeration=(term,)),
                    ),
                ),
                emission="guaranteed",
            ),
        ),
    )
    result = compile_model_shapes([model], [_binding()])
    assert result.source is not None and result.report.verified
    term._datatype = XSD.integer
    assert (
        None,
        RDF.first,
        literal_term("original", datatype=XSD.string),
    ) in result.source.graph()
    small = compile_model_shapes([model], [_binding()], limits=Limits(max_work=1))
    assert small.source is None and small.report.outcome == "exhausted"
    tail = compile_model_shapes(
        [model, replace(model, identifier="Other")],
        [_binding()],
        limits=Limits(max_work=1),
    )
    assert tail.report.requested == 2 and tail.report.checked == 0
    budget = Budget(Limits(max_issues=1))
    unsupported = replace(
        model,
        fields=(
            replace(model.fields[0], output=OutputRequirement()),
            replace(
                model.fields[0],
                name="other",
                predicate=str(EX.other),
                output=OutputRequirement(),
            ),
        ),
    )
    stopped = compile_model_shapes([unsupported], [_binding()], budget=budget)
    assert stopped.source is None and stopped.report.outcome == "exhausted"
    assert stopped.report.issues[0].code == "shacl.model_shape_unverified"
    huge = replace(model, identifier="x" * 1000)
    bounded = compile_model_shapes([huge], [_binding()], limits=Limits(max_bytes=100))
    assert bounded.source is None and bounded.report.outcome == "exhausted"
    long_name = "m" * 600
    long_field = replace(model.fields[0], name="f" * 600)
    joined = compile_model_shapes(
        [replace(model, identifier=long_name, fields=(long_field,))],
        [replace(_binding(), model=long_name)],
        limits=Limits(max_bytes=1000),
    )
    assert joined.source is None and joined.report.outcome == "exhausted"
    assert all(len(issue.location) <= 1000 for issue in joined.report.issues)
    selection = Report(
        "selection",
        1,
        tuple(
            Issue("consumer.selection_failed", "Known failure", str(index))
            for index in range(3)
        ),
    )
    selected = ValueEnumeration((EX.allowed,), report=selection)
    declared = replace(
        model,
        fields=(
            replace(
                model.fields[0],
                values=(
                    ValueDeclaration(
                        "iri", constraints=ValueConstraints(enumeration=selected)
                    ),
                ),
            ),
        ),
    )
    retained = compile_model_shapes(
        [declared], [_binding()], limits=Limits(max_issues=1)
    )
    assert retained.source is None and retained.report.outcome == "exhausted"
    assert [
        issue.location for issue in retained.report.issues if issue.kind == "finding"
    ] == ["0"]


def test_invalid_scope_and_recursive_child_obligations_are_explicit() -> None:
    """Required targets and child bindings cannot silently become empty shapes."""
    model = ModelDeclaration("Record", (), ())
    for models, bindings in (
        ([], [_binding()]),
        ([model], []),
        ([model], [replace(_binding(), focus_nodes=())]),
        (cast(object, None), [_binding()]),
        ([model], cast(object, iter([_binding()]))),
    ):
        with pytest.raises(InputError):
            compile_model_shapes(models, bindings)  # type: ignore[arg-type]
    optional = compile_model_shapes([model], [_binding()])
    assert optional.source is not None and not optional.report.verified
    assert "subject occurrence" in optional.report.issues[0].message
    native = check_rdf_emission(
        prepare_rdf(Graph()), [model], [RdfModelUsage("Record", EX.record)]
    )
    assert native.outcome == "unverified"
    if importlib.util.find_spec("pyshacl") is not None:
        assert validate_shapes(Graph(), optional.source).conforms is True
    recursive = replace(
        model,
        fields=(
            FieldDeclaration(
                "child", str(EX.child), (ValueDeclaration("object", model="Record"),)
            ),
        ),
    )
    result = compile_model_shapes([recursive], [_binding()])
    assert result.source is not None and result.report.outcome == "unverified"
    assert "recursive" in result.report.issues[0].message
