"""Multilingual source traits and explicit native encodings retain their limits."""

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.codecs import models_from_data
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
    ValueDeclaration as Value,
)
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.models.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.serialization import check_instance
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage

EX = Namespace("https://language.example/")
DIRECTION = URIRef(str(RDF) + "direction")
LANGUAGE = URIRef(str(RDF) + "language")


def _model(*values: Value, output: Output | None = None) -> Model:
    """Require one multilingual field while keeping its RDF representation explicit."""
    return Model(
        "record",
        (str(EX.Record),),
        (Field("label", str(EX.label), values, emission="guaranteed", output=output),),
    )


def _document(value: object, context: object | None = None) -> dict[str, object]:
    """Supply the actual selected field without inferring language-map keys."""
    return {
        "@id": str(EX.root),
        "@type": str(EX.Record),
        "label": value,
        "@context": context if context is not None else {"label": str(EX.label)},
    }


def _vocabulary() -> Graph:
    """Declare a generic property without promising a physical datatype range."""
    return (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.label, RDF.type, RDF.Property))
    )


def test_absence_codec_validation_and_existing_positional_fields() -> None:
    model = models_from_data(
        [
            {
                "identifier": "record",
                "types": [str(EX.Record)],
                "fields": [
                    {
                        "name": "label",
                        "predicate": str(EX.label),
                        "values": [
                            {
                                "kind": "literal",
                                "language": {"absent": True},
                                "direction": {"absent": True},
                            }
                        ],
                    }
                ],
            }
        ]
    )[0]
    branch = model.fields[0].values[0]
    assert branch.language is ABSENT and branch.direction is ABSENT
    assert Value("literal", None, "EN").language == "EN"
    invalid: tuple[dict[str, object], ...] = (
        {"language": 1},
        {"direction": []},
        {"direction": "up"},
        {"kind": "iri", "direction": ABSENT},
        {"language": ABSENT, "datatype": str(RDF.langString)},
    )
    for kwargs in invalid:
        with pytest.raises(InputError):
            Value(**kwargs)  # type: ignore[arg-type]
    bad = [
        {
            "identifier": "record",
            "fields": [
                {
                    "name": "label",
                    "predicate": str(EX.label),
                    "values": [{"kind": "literal", "direction": {"absent": False}}],
                }
            ],
        }
    ]
    with pytest.raises(InputError):
        models_from_data(bad)


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_compact_maps_and_explicit_values_have_the_same_effective_contract(
    policy: DirectionPolicy,
) -> None:
    model = _model(
        Value("literal", language="EN", direction="rtl"),
        Value("literal", language="ar", direction="rtl"),
        Value("literal", language=ABSENT, direction="rtl"),
    )
    mapping = _document(
        {"EN": ["one", "two"], "ar": "اسم", "@none": "neutral"},
        {
            "label": {
                "@id": str(EX.label),
                "@container": "@language",
                "@direction": "rtl",
            }
        },
    )
    explicit = _document(
        [
            {"@value": "one", "@language": "en", "@direction": "rtl"},
            {"@value": "two", "@language": "EN", "@direction": "rtl"},
            {"@value": "اسم", "@language": "ar", "@direction": "rtl"},
            {"@value": "neutral", "@direction": "rtl"},
        ]
    )
    for document in (mapping, explicit):
        assert check_emission(
            document, [model], [ModelUsage("record")], rdf_direction=policy
        ).verified
        assert not check_emission(document, [model], [ModelUsage("record")]).verified
    for mutation in (
        {"@value": "one", "@language": "fr", "@direction": "rtl"},
        {"@value": "one", "@language": "en", "@direction": "ltr"},
    ):
        report = check_emission(
            _document(mutation), [model], [ModelUsage("record")], rdf_direction=policy
        )
        assert any(issue.code == "emission.value_mismatch" for issue in report.issues)


def test_null_overrides_and_unchecked_vs_required_absence() -> None:
    context = {"@language": "en", "@direction": "rtl", "label": str(EX.label)}
    absent = _document(
        "plain",
        {
            **context,
            "label": {
                "@id": str(EX.label),
                "@language": None,
                "@direction": None,
            },
        },
    )
    model = _model(Value("literal", language=ABSENT, direction=ABSENT))
    assert check_emission(absent, [model], [ModelUsage("record")]).verified
    inherited = _document("plain", context)
    assert check_emission(
        inherited,
        [_model(Value("literal"))],
        [ModelUsage("record")],
        rdf_direction="i18n-datatype",
    ).verified
    required = check_emission(
        inherited, [model], [ModelUsage("record")], rdf_direction="i18n-datatype"
    )
    assert required.outcome == "disagreement"


@pytest.mark.parametrize("policy", ["i18n-datatype", "compound-literal"])
def test_actual_serializer_once_and_native_conversion_verify_finite_profile(
    policy: DirectionPolicy,
) -> None:
    model = _model(Value("literal", language="ar", direction="rtl"))
    document = _document({"@value": "اسم", "@language": "AR", "@direction": "rtl"})
    calls = []

    def serializer(value: object, *, compact: bool) -> object:
        calls.append(compact)
        return value

    result = check_instance(
        document,
        serializer,
        [model],
        [ModelUsage("record")],
        vocabulary=_vocabulary(),
        options={"compact": True},
        rdf_direction=policy,
    )
    assert result.report.verified and result.output == document and calls == [True]
    prepared = prepare_jsonld(document, direction=policy)
    assert prepared.source is not None and prepared.report.verified
    native = check_rdf_emission(
        prepared.source,
        [model],
        [RdfModelUsage("record", EX.root)],
        rdf_direction=policy,
    )
    assert native.verified
    assert not check_rdf_emission(
        prepared.source, [model], [RdfModelUsage("record", EX.root)]
    ).verified
    explicit_datatype = _model(
        Value("literal", language="ar", direction="rtl", datatype=str(RDF.langString))
    )
    assert not check_rdf_emission(
        prepared.source,
        [explicit_datatype],
        [RdfModelUsage("record", EX.root)],
        rdf_direction=policy,
    ).verified
    assert not check_models(
        [explicit_datatype], _vocabulary(), rdf_direction=policy
    ).verified
    constrained = _vocabulary().add((EX.label, RDFS.range, RDF.langString))
    assert not check_models([model], constrained, rdf_direction=policy).verified
    if policy == "compound-literal":
        data_property = _vocabulary().add((EX.label, RDF.type, OWL.DatatypeProperty))
        assert any(
            issue.code == "models.value_kind_conflict"
            for issue in check_models(
                [model], data_property, rdf_direction=policy
            ).issues
        )


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "language", "direction", "extra", "shared"]
)
def test_native_compound_components_must_be_unambiguous_in_the_selected_graph(
    mutation: str,
) -> None:
    node = BNode()
    graph = Graph().add((EX.root, RDF.type, EX.Record)).add((EX.root, EX.label, node))
    graph.add((node, RDF.value, Literal("text")))
    graph.add((node, LANGUAGE, Literal("en")))
    graph.add((node, DIRECTION, Literal("rtl")))
    if mutation == "missing":
        graph.remove((node, DIRECTION, None))
    elif mutation == "duplicate":
        graph.add((node, RDF.value, Literal("other")))
    elif mutation == "language":
        graph.set((node, LANGUAGE, Literal("bad_tag")))
    elif mutation == "direction":
        graph.set((node, DIRECTION, Literal("up")))
    elif mutation == "extra":
        graph.add((node, EX.annotation, Literal("extra")))
    else:
        graph.add((EX.other, EX.label, node))
    report = check_rdf_emission(
        prepare_source(graph),
        [_model(Value("literal", language="en", direction="rtl"))],
        [RdfModelUsage("record", EX.root)],
        rdf_direction="compound-literal",
    )
    assert report.outcome == "unverified"


def test_native_named_scope_and_physical_counts_preserve_opposite_directions() -> None:
    model = _model(
        Value("literal", language="en"),
        output=Output(minimum_values=2, maximum_values=2),
    )
    document = _document(
        [
            {"@value": "same", "@language": "en", "@direction": direction}
            for direction in ("ltr", "rtl")
        ]
    )
    source = check_emission(
        document, [model], [ModelUsage("record")], rdf_direction="i18n-datatype"
    )
    assert source.outcome == "unverified"
    assert any(
        issue.code == "emission.cardinality_unverified" for issue in source.issues
    )
    for policy in ("i18n-datatype", "compound-literal"):
        # Supply independent actual RDF: the maintained converter's direction
        # deduplication boundary must not turn this physical count into one.
        graph = Graph().add((EX.root, RDF.type, EX.Record))
        for direction in ("ltr", "rtl"):
            if policy == "i18n-datatype":
                graph.add(
                    (
                        EX.root,
                        EX.label,
                        Literal(
                            "same",
                            datatype=URIRef(
                                "https://www.w3.org/ns/i18n#en_" + direction
                            ),
                            normalize=False,
                        ),
                    )
                )
            else:
                node = BNode()
                graph.add((EX.root, EX.label, node))
                graph.add((node, RDF.value, Literal("same")))
                graph.add((node, LANGUAGE, Literal("en")))
                graph.add((node, DIRECTION, Literal(direction)))
        assert check_rdf_emission(
            prepare_source(graph),
            [model],
            [RdfModelUsage("record", EX.root)],
            rdf_direction=policy,
        ).verified
    dataset = Dataset()
    node = BNode()
    selected = dataset.graph(EX.selected)
    selected.add((EX.root, RDF.type, EX.Record)).add((EX.root, EX.label, node))
    other = dataset.graph(EX.other)
    other.add((node, RDF.value, Literal("text"))).add((node, DIRECTION, Literal("rtl")))
    report = check_rdf_emission(
        prepare_source(dataset),
        [_model(Value("literal", direction="rtl"))],
        [RdfModelUsage("record", EX.root)],
        graph_name=str(EX.selected),
        rdf_direction="compound-literal",
    )
    assert report.outcome == "unverified"
    ordinary = (
        Graph()
        .add((EX.root, RDF.type, EX.Record))
        .add((EX.root, EX.label, Literal("plain")))
    )
    assert check_rdf_emission(
        prepare_source(ordinary),
        [_model(Value("literal", language=ABSENT, direction=ABSENT))],
        [RdfModelUsage("record", EX.root)],
    ).verified


def test_selected_map_keys_require_explicit_coverage() -> None:
    from bijux_ontology.jsonld.checking import prepare_usages
    from bijux_ontology.jsonld.expectations import ContextExpectation
    from bijux_ontology.jsonld.selectors import KeyedItems, Property, SourceSelector

    context = {"label": {"@id": str(EX.label), "@container": "@language"}}
    selector = SourceSelector(
        (Property("label", minimum=1), KeyedItems(keys=("EN", "ar"), minimum=2))
    )
    for labels, expected in (
        ({"EN": "name", "ar": "اسم"}, True),
        ({"EN": "name"}, False),
    ):
        prepared = prepare_usages(_document(labels, context))
        selected = prepared.select(selector)
        assert selected.report.verified is expected
        if expected:
            assert selected.paths == (("label", "EN"), ("label", "ar"))
            assert prepared.check(
                [
                    ContextExpectation(("label", "EN"), str(EX.label), language="en"),
                    ContextExpectation(("label", "ar"), str(EX.label), language="ar"),
                ]
            ).verified


def test_native_encoded_metadata_and_tagless_direction() -> None:
    model = _model(Value("literal", language=ABSENT, direction="rtl"))
    for policy in ("i18n-datatype", "compound-literal"):
        document = _document({"@value": "plain", "@direction": "rtl"})
        prepared = prepare_jsonld(document, direction=policy)
        assert prepared.source is not None
        assert check_rdf_emission(
            prepared.source,
            [model],
            [RdfModelUsage("record", EX.root)],
            rdf_direction=policy,
        ).verified
    for suffix in ("en_left", "bad_tag_rtl", "rtl", "en_rtl_extra"):
        graph = (
            Graph()
            .add((EX.root, RDF.type, EX.Record))
            .add(
                (
                    EX.root,
                    EX.label,
                    Literal(
                        "plain", datatype=URIRef("https://www.w3.org/ns/i18n#" + suffix)
                    ),
                )
            )
        )
        invalid = check_rdf_emission(
            prepare_source(graph),
            [model],
            [RdfModelUsage("record", EX.root)],
            rdf_direction="i18n-datatype",
        )
        assert invalid.outcome == "unverified"


def test_direction_encoding_does_not_satisfy_an_incompatible_physical_datatype() -> (
    None
):
    document = _document({"@value": "plain", "@direction": "rtl"})
    model = _model(Value("literal", datatype="http://www.w3.org/2001/XMLSchema#string"))
    for policy in ("i18n-datatype", "compound-literal"):
        checked = check_instance(
            document,
            lambda value: value,
            [model],
            [ModelUsage("record")],
            vocabulary=_vocabulary(),
            options={},
            rdf_direction=policy,
        )
        assert checked.report.outcome == "disagreement"


def test_i18n_literal_samples_use_selected_physical_interpretation() -> None:
    sample = Literal(
        "name", datatype=URIRef("https://www.w3.org/ns/i18n#en_rtl"), normalize=False
    )
    model = _model(Value("literal", language="EN", direction="rtl", samples=(sample,)))
    assert check_models([model], _vocabulary(), rdf_direction="i18n-datatype").verified
    assert not check_models(
        [model], _vocabulary(), rdf_direction="compound-literal"
    ).verified


def test_unchecked_direction_cannot_promise_an_inferred_physical_range() -> None:
    model = _model(Value("literal", language="en"))
    vocabulary = _vocabulary().add((EX.label, RDF.type, OWL.DatatypeProperty))
    vocabulary.add((EX.label, RDFS.range, RDF.langString))
    document = _document({"@value": "name", "@language": "en", "@direction": "rtl"})
    for policy in ("i18n-datatype", "compound-literal"):
        checked = check_instance(
            document,
            lambda value: value,
            [model],
            [ModelUsage("record")],
            vocabulary=vocabulary,
            options={},
            rdf_direction=policy,
        )
        assert checked.report.outcome == "unverified"
    absent = _model(Value("literal", language="en", direction=ABSENT))
    ordinary = _document({"@value": "name", "@language": "en"})
    assert check_instance(
        ordinary,
        lambda value: value,
        [absent],
        [ModelUsage("record")],
        vocabulary=vocabulary,
        options={},
        rdf_direction="compound-literal",
    ).report.verified
