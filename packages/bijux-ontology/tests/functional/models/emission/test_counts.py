"""Application output counts distinguish RDF values from ordered positions."""

import json
from dataclasses import asdict, replace

import pytest
from rdflib import RDF, BNode, Graph, Literal, URIRef

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import OutputRequirement
from bijux_ontology.errors import InputError
from bijux_ontology.models import ModelUsage, check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

EX = "https://example.org/"


def model(requirement: OutputRequirement, *, listed: bool = False) -> ModelDeclaration:
    """Use one explicit output policy independent of Python constructor rules."""
    return ModelDeclaration(
        "record",
        (EX + "Record",),
        (
            FieldDeclaration(
                "values",
                EX + "values",
                (ValueDeclaration("literal", container="@list" if listed else None),),
                output=requirement,
            ),
        ),
    )


def document(value: object, *, listed: bool = False) -> dict[str, object]:
    """Keep scalar and list fixtures identical apart from explicit collection scope."""
    return {
        "@context": {"@vocab": EX},
        "@type": "Record",
        "values": {"@list": value} if listed else value,
    }


def test_two_list_positions_differ_from_two_distinct_unordered_values() -> None:
    unordered = model(OutputRequirement(minimum_values=2, maximum_values=2))
    repeated = check_emission(document([1, 1]), [unordered], [ModelUsage("record")])
    assert [(issue.code, issue.actual) for issue in repeated.issues] == [
        ("emission.value_count_mismatch", "1")
    ]
    assert check_emission(
        document([1, 2]), [unordered], [ModelUsage("record")]
    ).verified
    ordered = model(
        OutputRequirement(
            minimum_values=1,
            maximum_values=1,
            minimum_list_members=2,
            maximum_list_members=2,
        ),
        listed=True,
    )
    assert check_emission(
        document([1, 1], listed=True), [ordered], [ModelUsage("record")]
    ).verified
    short = check_emission(
        document([1], listed=True), [ordered], [ModelUsage("record")]
    )
    assert "emission.list_length_mismatch" in {issue.code for issue in short.issues}


def test_empty_lists_emit_one_rdf_nil_value_and_zero_members() -> None:
    expected = model(
        OutputRequirement(
            minimum_values=1,
            maximum_values=1,
            minimum_list_members=0,
            maximum_list_members=0,
        ),
        listed=True,
    )
    assert check_emission(
        document([], listed=True), [expected], [ModelUsage("record")]
    ).verified
    # Two source occurrences of rdf:nil remain one RDF object for this predicate.
    assert check_emission(
        document([{"@list": []}, {"@list": []}]), [expected], [ModelUsage("record")]
    ).verified
    mixed = replace(
        expected,
        fields=(
            replace(
                expected.fields[0],
                values=(*expected.fields[0].values, ValueDeclaration("iri")),
                output=OutputRequirement(minimum_values=1, maximum_values=1),
            ),
        ),
    )
    assert check_emission(
        document([{"@list": []}, {"@id": str(RDF.nil)}]),
        [mixed],
        [ModelUsage("record")],
    ).verified
    absent = check_emission(document([]), [expected], [ModelUsage("record")])
    assert "emission.list_required" in {issue.code for issue in absent.issues}


@pytest.mark.parametrize("value", [None, []])
def test_source_null_empty_and_omission_are_explicit_separate_contracts(
    value: object,
) -> None:
    required = model(OutputRequirement(require_property=True, minimum_values=1))
    result = check_emission(document(value), [required], [ModelUsage("record")])
    assert not result.verified
    assert "emission.value_count_mismatch" in {issue.code for issue in result.issues}
    missing = document(value)
    missing.pop("values")
    assert "emission.required_occurrence_missing" in {
        issue.code
        for issue in check_emission(missing, [required], [ModelUsage("record")]).issues
    }
    permitted = model(
        OutputRequirement(
            require_property=True, allow_null=True, allow_empty=True, maximum_values=0
        )
    )
    assert check_emission(document(value), [permitted], [ModelUsage("record")]).verified


def test_each_actual_list_has_its_own_member_bounds() -> None:
    expected = model(
        OutputRequirement(minimum_list_members=2, maximum_list_members=2), listed=True
    )
    valid = document([{"@list": [1, 1]}, {"@list": [2, 3]}])
    assert check_emission(valid, [expected], [ModelUsage("record")]).verified
    invalid = document([{"@list": [1]}, {"@list": [1, 2, 3]}])
    result = check_emission(invalid, [expected], [ModelUsage("record")])
    assert [
        issue.actual
        for issue in result.issues
        if issue.code == "emission.list_length_mismatch"
    ] == ["1", "3"]


def test_optional_list_member_bounds_do_not_require_an_occurrence() -> None:
    expected = model(OutputRequirement(maximum_list_members=1), listed=True)
    expected = replace(expected, fields=(replace(expected.fields[0], optional=True),))
    subject = URIRef(EX + "record")
    predicate = URIRef(EX + "values")
    for values in (None, (), (1,), (1, 2), "scalar"):
        data = document(
            list(values) if isinstance(values, tuple) else values, listed=True
        )
        data["@id"] = str(subject)
        graph = Graph().add((subject, RDF.type, URIRef(EX + "Record")))
        if values is None:
            data.pop("values")
        elif values == "scalar":
            data["values"] = "scalar"
            graph.add((subject, predicate, Literal("scalar")))
        else:
            assert isinstance(values, tuple)
            head = BNode() if values else RDF.nil
            graph.add((subject, predicate, head))
            current = head
            for index, value in enumerate(values):
                following = BNode() if index + 1 < len(values) else RDF.nil
                graph.add((current, RDF.first, Literal(value)))
                graph.add((current, RDF.rest, following))
                current = following
        source = check_emission(data, (expected,), (ModelUsage("record"),))
        native = check_rdf_emission(
            prepare_rdf(RdfSource(graph)),
            (expected,),
            (RdfModelUsage("record", subject),),
        )
        for result in (source, native):
            assert result.verified is (values in (None, (), (1,))), result.to_dict()


def test_legacy_optional_is_an_optional_emission_alias_and_counts_roundtrip() -> None:
    field = FieldDeclaration(
        "values", EX + "values", (ValueDeclaration("literal"),), optional=True
    )
    assert field.emission == "optional"
    assert (
        FieldDeclaration(
            "values", EX + "values", (ValueDeclaration("literal"),)
        ).emission
        == "unknown"
    )
    with pytest.raises(InputError, match="conflicts"):
        FieldDeclaration(
            "values", EX + "values", field.values, optional=True, emission="guaranteed"
        )
    expected = model(OutputRequirement(require_property=True, minimum_values=2))
    assert models_from_data(json.loads(json.dumps([asdict(expected)]))) == (expected,)
    for value in (True, -1, "2"):
        raw = json.loads(json.dumps(asdict(expected)))
        raw["fields"][0]["output"]["minimum_values"] = value
        with pytest.raises(InputError):
            models_from_data([raw])


@pytest.mark.parametrize("listed", [False, True])
def test_joined_entity_counts_include_deeper_descriptions_without_widening_source(
    listed: bool,
) -> None:
    requirement = (
        OutputRequirement(maximum_list_members=1)
        if listed
        else OutputRequirement(maximum_values=1)
    )
    expected = model(requirement, listed=listed)
    value = {"@list": [1]} if listed else [1]
    deeper = {"@list": [1, 2]} if listed else [2]
    data = {
        **document(value),
        "@id": EX + "record",
        "related": {"@id": EX + "record", "values": deeper},
    }
    assert check_emission(data, [expected], [ModelUsage("record")]).verified
    entity = check_emission(
        data,
        [expected],
        [ModelUsage("record", reference=EX + "record", graph=None)],
    )
    code = (
        "emission.list_length_mismatch" if listed else "emission.value_count_mismatch"
    )
    assert any(issue.code == code and issue.actual == "2" for issue in entity.issues)
