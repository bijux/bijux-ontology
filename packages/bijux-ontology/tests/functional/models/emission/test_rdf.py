"""Real RDF and JSON-LD serializers share native output contract outcomes."""

from dataclasses import dataclass, field, replace

import pytest
from pydantic import BaseModel, FieldSerializationInfo, field_serializer
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import RDF, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration as Field,
)
from bijux_ontology.declarations.types import (
    ModelDeclaration as Model,
)
from bijux_ontology.declarations.types import (
    OutputRequirement as Output,
)
from bijux_ontology.declarations.types import (
    SubjectIdentity,
)
from bijux_ontology.declarations.types import (
    ValueDeclaration as Value,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Limits
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.dataclasses import model_declarations
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("https://output.example/")


def test_iri_text_metadata_accepts_rdflib_terms_without_changing_output_identity() -> (
    None
):
    """Accepted IRI string subclasses describe the same asserted output contract."""
    model = Model(
        "record",
        (EX.Record,),
        (
            Field("child", EX.child, (Value("object", types=(EX.Child,)),)),
            Field("name", EX.name, (Value("literal", datatype=XSD.string),)),
        ),
        subject=SubjectIdentity(expected_iri=EX.record),
    )
    graph = (
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add((EX.record, EX.child, EX.child_record))
        .add((EX.child_record, RDF.type, EX.Child))
        .add((EX.record, EX.name, Literal("name")))
    )
    usages = (RdfModelUsage("record", EX.record),)
    source = prepare_source(RdfSource(graph))
    assert check_rdf_emission(source, (model,), usages).verified
    graph.set((EX.child_record, RDF.type, EX.Other))
    changed = check_rdf_emission(prepare_source(RdfSource(graph)), (model,), usages)
    assert any(issue.code == "emission.value_mismatch" for issue in changed.issues)


def _model(
    value: Value, *, output: Output | None = None, reverse: bool = False
) -> Model:
    """Declare one actual RDF field without Python-required or OWL assumptions."""
    return Model(
        "record",
        (str(EX.Record),),
        (
            Field(
                "values",
                str(EX.values),
                (value,),
                reverse=reverse,
                emission="guaranteed",
                output=output,
            ),
        ),
    )


def test_dataclass_rdflib_and_actual_pydantic_jsonld_serializers_agree() -> None:
    title_contract = RdfField(
        str(EX.title),
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
        emission="guaranteed",
    )

    @dataclass
    class Item:
        title: str = field(metadata={"rdf": title_contract})

    @dataclass
    class Catalogue:
        items: list[Item] = field(
            metadata={
                "rdf": RdfField(
                    str(EX.items),
                    (Value("object", model="item"),),
                    output_contract=True,
                    emission="guaranteed",
                )
            }
        )

    class JsonItem(BaseModel):
        title: str

        @field_serializer("title")
        def serialize_title(
            self, value: str, info: FieldSerializationInfo
        ) -> str | int:
            return len(value) if info.context and info.context.get("numeric") else value

    class JsonCatalogue(BaseModel):
        items: list[JsonItem]

    extraction = model_declarations(
        {Catalogue: (str(EX.Catalogue),), Item: (str(EX.Item),)},
        identifiers={Catalogue: "catalogue", Item: "item"},
    )
    assert extraction.report.verified
    record = Catalogue([Item("First"), Item("Second")])
    json_record = JsonCatalogue(
        items=[JsonItem(title=item.title) for item in record.items]
    )
    for numeric in (False, True):
        graph = Graph().add((EX.root, RDF.type, EX.Catalogue))
        output = json_record.model_dump(mode="json", context={"numeric": numeric})
        output.update(
            {
                "@id": str(EX.root),
                "@type": str(EX.Catalogue),
                "@context": {"items": str(EX.items), "title": str(EX.title)},
            }
        )
        for index, item in enumerate(record.items):
            child = EX[f"item/{index}"]
            graph.add((EX.root, EX.items, child))
            graph.add((child, RDF.type, EX.Item))
            graph.add(
                (child, EX.title, Literal(len(item.title) if numeric else item.title))
            )
            output["items"][index].update({"@id": str(child), "@type": str(EX.Item)})
        native = check_rdf_emission(
            prepare_source(graph),
            extraction.declarations,
            [RdfModelUsage("catalogue", EX.root)],
        )
        expanded = check_emission(
            output, extraction.declarations, [ModelUsage("catalogue")]
        )
        assert native.verified is expanded.verified is (not numeric)
        assert {issue.code for issue in native.issues} == {
            issue.code for issue in expanded.issues
        }
        if numeric:
            assert {issue.code for issue in native.issues} == {
                "emission.value_mismatch"
            }


def test_reverse_endpoints_and_selected_graphs_do_not_import_other_subject_facts() -> (
    None
):
    model = _model(Value("iri", types=(str(EX.Work),)), reverse=True)
    dataset = Dataset()
    selected = dataset.graph(EX.selected)
    selected.add((EX.root, RDF.type, EX.Record))
    selected.add((EX.work, RDF.type, EX.Work))
    selected.add((EX.work, EX.values, EX.root))
    selected.add((EX.root, EX.values, EX.unrelated))
    dataset.graph(EX.other).add((EX.unrelated, RDF.type, EX.Work))
    dataset.graph(EX.other).add((EX.unrelated, EX.values, EX.root))
    prepared = prepare_source(dataset)
    assert check_rdf_emission(
        prepared,
        [model],
        [RdfModelUsage("record", EX.root)],
        graph_name=str(EX.selected),
    ).verified
    with pytest.raises(InputError, match="graph name"):
        check_rdf_emission(prepared, [model], [RdfModelUsage("record", EX.root)])
    selected.remove((EX.work, EX.values, EX.root))
    missing = check_rdf_emission(
        prepare_source(dataset),
        [model],
        [RdfModelUsage("record", EX.root)],
        graph_name=str(EX.selected),
    )
    assert {issue.code for issue in missing.issues} == {
        "emission.required_entity_fact_missing"
    }
    absent = check_rdf_emission(
        prepared, [model], [RdfModelUsage("record", EX.root)], graph_name="@default"
    )
    assert {issue.code for issue in absent.issues} == {"emission.reference_unverified"}
    assert check_rdf_emission(
        prepared, [model], [RdfModelUsage("record", EX.root)], union=True
    ).verified


def test_owned_blank_subject_objects_are_required_even_when_labels_equal() -> None:
    blank = BNode()
    graph = Graph().add((blank, RDF.type, EX.Record))
    graph.add((blank, EX.values, Literal("Name")))
    source = prepare_source(graph)
    owned = next(source.graph().subjects(RDF.type, EX.Record))
    assert isinstance(owned, BNode)
    model = _model(Value("literal", datatype=str(XSD.string)))
    assert check_rdf_emission(
        source, [model], [RdfModelUsage("record", owned)]
    ).verified
    foreign = BNode(str(owned))
    assert foreign == owned and foreign is not owned
    unverified = check_rdf_emission(
        source,
        [model],
        [RdfModelUsage("record", owned), RdfModelUsage("record", foreign)],
    )
    assert {issue.code for issue in unverified.issues} == {"emission.node_unverified"}
    named_only = replace(model, subject=SubjectIdentity(allow_blank=False))
    assert "emission.subject_mismatch" in {
        issue.code
        for issue in check_rdf_emission(
            source, [named_only], [RdfModelUsage("record", owned)]
        ).issues
    }


def test_rdf_lists_count_positions_distinct_heads_and_empty_nil_exactly() -> None:
    graph = Graph().add((EX.root, RDF.type, EX.Record))
    head = BNode()
    graph.add((EX.root, EX.values, head))
    Collection(graph, head, [Literal("repeated"), Literal("repeated")])
    model = _model(
        Value("literal", container="@list"),
        output=Output(
            minimum_values=1,
            maximum_values=1,
            minimum_list_members=2,
            maximum_list_members=2,
        ),
    )
    assert check_rdf_emission(
        prepare_source(graph), [model], [RdfModelUsage("record", EX.root)]
    ).verified
    short = replace(
        model, fields=(replace(model.fields[0], output=Output(maximum_list_members=1)),)
    )
    assert [
        (issue.code, issue.actual)
        for issue in check_rdf_emission(
            prepare_source(graph), [short], [RdfModelUsage("record", EX.root)]
        ).issues
    ] == [("emission.list_length_mismatch", "2")]
    graph.remove((EX.root, EX.values, None))
    graph.add((EX.root, EX.values, RDF.nil))
    empty = replace(
        model,
        fields=(
            replace(
                model.fields[0],
                output=Output(
                    minimum_values=1,
                    maximum_values=1,
                    minimum_list_members=0,
                    maximum_list_members=0,
                ),
            ),
        ),
    )
    assert check_rdf_emission(
        prepare_source(graph), [empty], [RdfModelUsage("record", EX.root)]
    ).verified


@pytest.mark.parametrize("malformation", ["cycle", "fork"])
def test_malformed_lists_never_pass_partial_lengths(malformation: str) -> None:
    graph = Graph().add((EX.root, RDF.type, EX.Record))
    head = BNode()
    graph.add((EX.root, EX.values, head))
    graph.add((head, RDF.first, Literal("a")))
    graph.add((head, RDF.rest, head if malformation == "cycle" else RDF.nil))
    if malformation == "fork":
        graph.add((head, RDF.first, Literal("b")))
    model = _model(
        Value("literal", container="@list"), output=Output(minimum_list_members=1)
    )
    result = check_rdf_emission(
        prepare_source(graph), [model], [RdfModelUsage("record", EX.root)]
    )
    assert not result.verified
    assert {issue.kind for issue in result.issues} == {"unverified"}
    assert "emission.cardinality_unverified" in {issue.code for issue in result.issues}
    bounded = check_rdf_emission(
        prepare_source(graph),
        [model],
        [RdfModelUsage("record", EX.root)],
        limits=Limits(max_work=1),
    )
    assert not bounded.complete and bounded.outcome == "exhausted"


def test_native_literal_identity_source_fidelity_and_source_presence_are_separate() -> (
    None
):
    graph = Graph().add((EX.root, RDF.type, EX.Record))
    graph.add(
        (EX.root, EX.values, Literal("01", datatype=XSD.integer, normalize=False))
    )
    graph.add((EX.root, EX.values, Literal("1", datatype=XSD.integer, normalize=False)))
    model = _model(
        Value("literal", datatype=str(XSD.integer)),
        output=Output(minimum_values=2, maximum_values=2),
    )
    assert check_rdf_emission(
        prepare_source(graph), [model], [RdfModelUsage("record", EX.root)]
    ).verified
    source_only = replace(
        model, fields=(replace(model.fields[0], output=Output(require_property=True)),)
    )
    assert {
        issue.code
        for issue in check_rdf_emission(
            prepare_source(graph), [source_only], [RdfModelUsage("record", EX.root)]
        ).issues
    } == {"emission.source_presence_unverified"}
    graph.remove((EX.root, EX.values, None))
    graph.add((EX.root, EX.values, Literal("opaque", datatype=EX.Custom)))
    unsupported = check_rdf_emission(
        prepare_source(graph),
        [_model(Value("literal"))],
        [RdfModelUsage("record", EX.root)],
    )
    assert {issue.code for issue in unsupported.issues} == {
        "literal.datatype_unverified"
    }
    normalized = prepare_source(
        RdfSource(
            "<https://output.example/root> a <https://output.example/Record>; "
            '<https://output.example/values> "01"^^'
            "<http://www.w3.org/2001/XMLSchema#integer>.",
            format="turtle",
        ),
        preserve_literals=False,
    )
    result = check_rdf_emission(normalized, [model], [RdfModelUsage("record", EX.root)])
    assert "literal.lexical_unverified" in {issue.code for issue in result.issues}
    assert "emission.cardinality_unverified" in {issue.code for issue in result.issues}


def test_missing_and_ambiguous_model_bindings_remain_explicit() -> None:
    graph = Graph().add((EX.root, RDF.type, EX.Record))
    graph.add((EX.root, EX.values, EX.child))
    graph.add((EX.child, RDF.type, EX.A))
    graph.add((EX.child, RDF.type, EX.B))
    root = _model(Value("object", model="a"))
    root = replace(
        root,
        fields=(
            replace(
                root.fields[0],
                values=(Value("object", model="a"), Value("object", model="b")),
            ),
        ),
    )
    declarations = [root, Model("a", (str(EX.A),)), Model("b", (str(EX.B),))]
    source = prepare_source(graph)
    assert {
        issue.code
        for issue in check_rdf_emission(
            source, declarations, [RdfModelUsage("record", EX.root)]
        ).issues
    } == {"emission.nested_scope_unverified"}
    assert check_rdf_emission(
        source,
        declarations,
        [RdfModelUsage("record", EX.root), RdfModelUsage("a", EX.child)],
    ).verified
    assert {
        issue.code for issue in check_rdf_emission(source, declarations, []).issues
    } == {"emission.node_unverified"}
    assert {
        issue.code
        for issue in check_rdf_emission(
            source, declarations, [RdfModelUsage("record", EX.missing)]
        ).issues
    } == {"emission.reference_unverified"}


def test_unknown_usage_model_is_input_error_independent_of_blank_ownership() -> None:
    source = prepare_source(Graph())
    report = check_rdf_emission(
        source, [], [RdfModelUsage("unknown", BNode("foreign"))]
    )
    assert [issue.code for issue in report.issues] == ["input.model_reference_missing"]


def test_owned_blank_subjects_keep_identity_across_explicit_union_selection() -> None:
    dataset = Dataset()
    blank = BNode()
    dataset.graph(EX.selected).add((blank, RDF.type, EX.Record))
    dataset.graph(EX.other).add((blank, EX.values, Literal("Name")))
    source = prepare_source(dataset)
    owned = next(source.graph(str(EX.selected)).subjects(RDF.type, EX.Record))
    assert isinstance(owned, BNode)
    expected = _model(Value("literal", datatype=str(XSD.string)))
    assert check_rdf_emission(
        source, [expected], [RdfModelUsage("record", owned)], union=True
    ).verified
    selected = check_rdf_emission(
        source,
        [expected],
        [RdfModelUsage("record", owned)],
        graph_name=str(EX.selected),
    )
    assert "emission.required_entity_fact_missing" in {
        issue.code for issue in selected.issues
    }
