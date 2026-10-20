"""Selected opaque field checks share native JSON/markup evaluation across routes."""

from dataclasses import replace
from decimal import Decimal

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.literal_contracts import MarkupLiteralContract
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.literal_values.json.types import (
    JsonLiteralContract,
    JsonRequirement,
)
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("urn:opaque-field:")


def _model(
    contract: JsonLiteralContract | MarkupLiteralContract, datatype: str = str(RDF.JSON)
) -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "payload",
                str(EX.payload),
                (ValueDeclaration("literal", datatype),),
                literal_contract=contract,
            ),
        ),
    )


def _ontology(datatype: str = str(RDF.JSON)) -> Graph:
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.payload, RDF.type, OWL.DatatypeProperty))
        .add(
            (
                RDF.JSON if datatype == str(RDF.JSON) else RDF.XMLLiteral,
                RDF.type,
                RDFS.Datatype,
            )
        )
        .add(
            (
                EX.payload,
                RDFS.range,
                RDF.JSON if datatype == str(RDF.JSON) else RDF.XMLLiteral,
            )
        )
    )


def test_json_field_obligation_checks_actual_structured_and_lexical_values() -> None:
    contract = JsonLiteralContract(
        (
            JsonRequirement(
                ("n",), kind="number", minimum=Decimal("1"), expected_json="1"
            ),
        )
    )
    model = _model(contract)
    document = {
        "@context": {"payload": {"@id": str(EX.payload), "@type": "@json"}},
        "@type": str(EX.Record),
        "payload": {"n": 1.0},
    }
    result = check_emission(document, (model,), (ModelUsage("record"),))
    assert result.verified, result.to_dict()
    no_obligation = replace(
        model, fields=(replace(model.fields[0], literal_contract=None),)
    )
    assert not check_emission(
        document, (no_obligation,), (ModelUsage("record"),)
    ).verified
    source = (
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add(
            (
                EX.record,
                EX.payload,
                Literal('{"n":1e0}', datatype=RDF.JSON, normalize=False),
            )
        )
    )
    native = check_rdf_emission(
        prepare_source(source), (model,), (RdfModelUsage("record", EX.record),)
    )
    assert native.verified, native.to_dict()
    assert str(source.value(EX.record, EX.payload)) == '{"n":1e0}'
    bad_value = check_emission(
        document | {"payload": {"n": 0}}, (model,), (ModelUsage("record"),)
    )
    assert any(
        issue.code == "literal.json_mismatch" and "payload" in issue.location
        for issue in bad_value
    )
    source.set(
        (EX.record, EX.payload, Literal("{broken", datatype=RDF.JSON, normalize=False))
    )
    malformed = check_rdf_emission(
        prepare_source(source), (model,), (RdfModelUsage("record", EX.record),)
    )
    assert malformed.outcome == "input_error"
    assert any("payload" in issue.location for issue in malformed)
    invalid_schema = _model(
        JsonLiteralContract(
            schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "urn:schema:invalid",
                "type": 17,
            },
            dialect="https://json-schema.org/draft/2020-12/schema",
        )
    )
    invalid = check_emission(document, (invalid_schema,), (ModelUsage("record"),))
    assert invalid.outcome == "input_error"
    unsupported = _model(
        JsonLiteralContract(schema=True, dialect="urn:unsupported-dialect")
    )
    unknown = check_emission(document, (unsupported,), (ModelUsage("record"),))
    assert not unknown.complete and not unknown.verified
    assert all(issue.kind != "input_error" for issue in unknown)


def test_opaque_field_preparation_owns_schema_and_reuses_markup_comparison() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:schema:record",
        "type": "object",
        "required": ["n"],
    }
    model = _model(
        JsonLiteralContract(
            schema=schema, dialect="https://json-schema.org/draft/2020-12/schema"
        )
    )
    plan = PreparedModels((model,), _ontology())
    schema["required"] = ["changed"]
    document = {
        "@type": str(EX.Record),
        str(EX.payload): {"@value": {"n": 1}, "@type": "@json"},
    }
    assert plan.check(document, (ModelUsage("record"),)).verified
    from bijux_ontology.models.persistence.loading import (
        compile_model_artifact,
        load_model_artifact,
    )

    artifact = compile_model_artifact(
        (model,), RdfSource(_ontology().serialize(format="turtle"), format="turtle")
    )
    assert artifact.report.verified and artifact.content is not None
    assert artifact.sha256 is not None and artifact.identity is not None
    restored = load_model_artifact(
        artifact.content, expected=artifact.identity, trusted_sha256=artifact.sha256
    )
    assert restored.prepared is not None and restored.report.verified
    assert restored.prepared.check(document, (ModelUsage("record"),)).verified
    expected = Literal('<b a="1" z="2"/>', datatype=RDF.XMLLiteral, normalize=False)
    actual = Literal('<b z="2" a="1"></b>', datatype=RDF.XMLLiteral, normalize=False)
    markup = _model(
        MarkupLiteralContract("xml-exc-c14n-with-comments", expected),
        str(RDF.XMLLiteral),
    )
    graph = (
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add((EX.record, EX.payload, actual))
    )
    result = check_rdf_emission(
        prepare_source(graph), (markup,), (RdfModelUsage("record", EX.record),)
    )
    assert actual != expected and result.verified, result.to_dict()
    assert graph.value(EX.record, EX.payload) is actual
