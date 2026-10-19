"""Explicit application predicates qualify actual values without rewriting RDF."""

from copy import deepcopy
from decimal import Decimal

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.codecs import models_from_data
from bijux_ontology.declarations.types import (
    EmissionGroup,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.serialization import check_instance
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.models.value_constraints.checking import application_matches
from bijux_ontology.rdf.terms import literal_term

EX = Namespace("https://example.org/")


def test_numeric_intervals_use_exact_values_and_enum_keeps_lexical_identity() -> None:
    lower = literal_term("01", datatype=str(XSD.integer))
    canonical = literal_term("1", datatype=str(XSD.integer))
    interval = ValueConstraints(minimum=Decimal("1.00"), maximum=Decimal(2))
    assert application_matches(interval, lower, budget=Budget()) is True
    assert (
        application_matches(
            interval, literal_term("2", datatype=str(XSD.decimal)), budget=Budget()
        )
        is True
    )
    assert (
        application_matches(
            interval, literal_term("2.01", datatype=str(XSD.decimal)), budget=Budget()
        )
        is False
    )
    assert (
        application_matches(
            ValueConstraints(minimum=Decimal(1), minimum_inclusive=False),
            lower,
            budget=Budget(),
        )
        is False
    )
    assert (
        application_matches(
            ValueConstraints(enumeration=(canonical,)), lower, budget=Budget()
        )
        is False
    )
    assert (
        application_matches(
            ValueConstraints(enumeration=(lower,)), lower, budget=Budget()
        )
        is True
    )
    assert str(lower) == "01" and lower.datatype == XSD.integer
    assert (
        application_matches(ValueConstraints(enumeration=()), None, budget=Budget())
        is False
    )


@pytest.mark.parametrize(
    "term",
    [
        literal_term("1E0", datatype=str(XSD.double)),
        literal_term("NaN", datatype=str(XSD.float)),
        literal_term("one", datatype=str(XSD.integer)),
        literal_term("1", datatype=str(EX.custom)),
        Literal("1"),
        EX.one,
    ],
)
def test_unsupported_numeric_conversions_remain_unknown(term: URIRef | Literal) -> None:
    assert (
        application_matches(ValueConstraints(minimum=Decimal(0)), term, budget=Budget())
        is None
    )


def test_original_code_point_lengths_and_explicit_re2_matching_modes() -> None:
    term = Literal("é1", lang="fr")
    assert (
        application_matches(
            ValueConstraints(min_length=2, max_length=2, pattern=r"\p{L}[0-9]"),
            term,
            budget=Budget(),
        )
        is True
    )
    assert (
        application_matches(ValueConstraints(pattern="1"), term, budget=Budget())
        is False
    )
    assert (
        application_matches(
            ValueConstraints(pattern="1", pattern_mode="search"), term, budget=Budget()
        )
        is True
    )
    assert (
        application_matches(
            ValueConstraints(max_length=1), Literal("e\u0301"), budget=Budget()
        )
        is False
    )
    assert str(term) == "é1" and term.language == "fr"
    for pattern in (r"(a)\1", r"(?=a)a", "["):
        assert (
            application_matches(
                ValueConstraints(pattern=pattern), Literal("aa"), budget=Budget()
            )
            is None
        )
    assert (
        application_matches(
            ValueConstraints(min_length=1),
            literal_term("7", datatype=str(XSD.integer)),
            budget=Budget(),
        )
        is None
    )
    # A definite independent violation survives an unsupported pattern.
    assert (
        application_matches(
            ValueConstraints(max_length=0, pattern=r"(?=a)a"),
            Literal("aa"),
            budget=Budget(),
        )
        is False
    )


def test_iri_namespaces_are_exact_original_spelling_prefixes() -> None:
    constraints = ValueConstraints(iri_namespaces=("https://example.org/concepts/",))
    for identifier, expected in (
        ("https://example.org/concepts/A", True),
        ("https://example.org/concepts/%41", True),
        ("https://example.org/concepts-extra/A", False),
        ("https://EXAMPLE.org/concepts/A", False),
    ):
        term = URIRef(identifier)
        assert application_matches(constraints, term, budget=Budget()) is expected
        assert str(term) == identifier
    assert (
        application_matches(
            constraints, Literal("https://example.org/concepts/A"), budget=Budget()
        )
        is False
    )
    assert (
        application_matches(
            ValueConstraints(enumeration=(EX.A,)), EX.B, budget=Budget()
        )
        is False
    )
    assert (
        application_matches(
            ValueConstraints(enumeration=(EX.A,)), None, budget=Budget()
        )
        is None
    )


def test_owned_predicates_use_the_cumulative_operation_budget() -> None:
    term = Literal("value")
    constraints = ValueConstraints(enumeration=(term,), pattern="value")
    budget = Budget(Limits(max_work=100))
    assert application_matches(constraints, term, budget=budget) is True
    with pytest.raises(LimitExceeded):
        for _ in range(10):
            application_matches(constraints, term, budget=budget)
    with pytest.raises(LimitExceeded):
        application_matches(
            ValueConstraints(pattern=".*"),
            Literal("x" * 30),
            budget=Budget(Limits(max_work=50)),
        )
    with pytest.raises(LimitExceeded):
        application_matches(
            ValueConstraints(enumeration=(EX.A, EX.B)),
            EX.A,
            budget=Budget(Limits(max_states=1)),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum": 0.0},
        {"minimum": Decimal("NaN")},
        {"minimum": Decimal(2), "maximum": Decimal(1)},
        {"min_length": True},
        {"max_length": -1},
        {"pattern_mode": "python"},
        {"enumeration": [EX.A]},
        {"enumeration": ("https://example.org/A",)},
        {"iri_namespaces": ("relative/",)},
        {"iri_namespaces": (BNode("urn:namespace:"),)},
    ],
)
def test_invalid_predicate_declarations_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(InputError):
        ValueConstraints(**kwargs)  # type: ignore[arg-type]


def _model(value: ValueDeclaration, *, groups: bool = False) -> ModelDeclaration:
    """Declare the consumer rule independently of actual serialized records."""
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("score", str(EX.score), (value,)),),
        emission_groups=(EmissionGroup("score-scope", ("score",)),) if groups else (),
    )


def test_actual_jsonld_and_rdf_apply_bounds_and_exact_enums_with_same_datatype() -> (
    None
):
    constraints = ValueConstraints(minimum=Decimal("1"), maximum=Decimal("2"))
    bounded = _model(
        ValueDeclaration("literal", str(XSD.integer), constraints=constraints)
    )
    enumerated = _model(
        ValueDeclaration(
            "literal",
            str(XSD.integer),
            constraints=ValueConstraints(
                enumeration=(literal_term("1", datatype=str(XSD.integer)),)
            ),
        )
    )
    for model, number, valid in (
        (bounded, 1, True),
        (bounded, 2, True),
        (bounded, 3, False),
        (enumerated, 1, True),
        (enumerated, 2, False),
    ):
        document = {
            "@context": {"@vocab": str(EX)},
            "@id": str(EX.record),
            "@type": "Record",
            "score": number,
        }
        before = deepcopy(document)
        graph = Graph()
        graph.add((EX.record, RDF.type, EX.Record))
        graph.add((EX.record, EX.score, Literal(number)))
        prepared = prepare_source(graph)
        reports = (
            check_emission(document, (model,), (ModelUsage("record"),)),
            check_rdf_emission(
                prepared, (model,), (RdfModelUsage("record", EX.record),)
            ),
        )
        assert all(report.verified is valid for report in reports)
        assert all(
            report.outcome == ("agreement" if valid else "disagreement")
            for report in reports
        )
        assert document == before


def test_entity_and_semantic_alias_numeric_matching_reads_actual_lexical_cache() -> (
    None
):
    value = ValueDeclaration(
        "literal", str(XSD.integer), constraints=ValueConstraints(maximum=Decimal("2"))
    )
    model = _model(value, groups=True)
    for number, valid in ((2, True), (3, False)):
        document = {
            "@context": {"@vocab": str(EX), "alias": str(EX.score)},
            "@id": str(EX.record),
            "@type": "Record",
            "alias": number,
        }
        source = check_emission(document, (model,), (ModelUsage("record"),))
        entity = check_emission(
            document,
            (model,),
            (ModelUsage("record", reference=str(EX.record), graph=None),),
        )
        assert source.verified is valid and entity.verified is valid
        assert (
            source.outcome
            == entity.outcome
            == ("agreement" if valid else "disagreement")
        )


def test_list_constraints_apply_to_members_and_skip_empty_list_sentinel() -> None:
    bounded = _model(
        ValueDeclaration(
            "literal",
            str(XSD.integer),
            container="@list",
            constraints=ValueConstraints(maximum=Decimal("2")),
        )
    )
    empty_enum = _model(
        ValueDeclaration(
            "literal",
            str(XSD.integer),
            container="@list",
            constraints=ValueConstraints(enumeration=()),
        )
    )
    for model, values, valid in (
        (bounded, (1, 2), True),
        (bounded, (1, 3), False),
        (empty_enum, (), True),
        (empty_enum, (1,), False),
    ):
        document = {
            "@context": {"@vocab": str(EX)},
            "@type": "Record",
            "score": {"@list": list(values)},
        }
        graph = Graph()
        graph.add((EX.record, RDF.type, EX.Record))
        head = BNode() if values else RDF.nil
        if values:
            Collection(graph, head, [Literal(value) for value in values])
        graph.add((EX.record, EX.score, head))
        source = check_emission(document, (model,), (ModelUsage("record"),))
        native = check_rdf_emission(
            prepare_source(graph), (model,), (RdfModelUsage("record", EX.record),)
        )
        assert source.verified is valid and native.verified is valid


def test_actual_pattern_conversion_and_directional_identity_stay_qualified() -> None:
    for constraints, raw in (
        (ValueConstraints(pattern=r"(?=a)a"), "aa"),
        (
            ValueConstraints(minimum=Decimal("0")),
            {"@value": "1E0", "@type": str(XSD.double)},
        ),
    ):
        model = _model(ValueDeclaration("literal", constraints=constraints))
        document = {"@context": {"@vocab": str(EX)}, "@type": "Record", "score": raw}
        assert (
            check_emission(document, (model,), (ModelUsage("record"),)).outcome
            == "unverified"
        )
    model = _model(
        ValueDeclaration(
            "literal",
            constraints=ValueConstraints(enumeration=(Literal("word", lang="en"),)),
        )
    )
    directional = {
        "@context": {"@vocab": str(EX)},
        "@type": "Record",
        "score": {"@value": "word", "@language": "en", "@direction": "rtl"},
    }
    assert (
        check_emission(
            directional,
            (model,),
            (ModelUsage("record"),),
            rdf_direction="i18n-datatype",
        ).outcome
        == "unverified"
    )


def test_actual_branch_alternatives_and_iri_subset_are_not_bypassed() -> None:
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "score",
                str(EX.score),
                (
                    ValueDeclaration(
                        "literal",
                        str(XSD.integer),
                        constraints=ValueConstraints(minimum=Decimal("10")),
                    ),
                    ValueDeclaration(
                        "literal",
                        str(XSD.string),
                        constraints=ValueConstraints(pattern="OK[0-9]+"),
                    ),
                    ValueDeclaration(
                        "iri",
                        constraints=ValueConstraints(
                            enumeration=(EX.allowed,), iri_namespaces=(str(EX),)
                        ),
                    ),
                ),
            ),
        ),
    )
    for raw, valid in (
        (5, False),
        (10, True),
        ("OK2", True),
        ("bad", False),
        ({"@id": str(EX.allowed)}, True),
        ({"@id": str(EX.other)}, False),
    ):
        document = {"@context": {"@vocab": str(EX)}, "@type": "Record", "score": raw}
        report = check_emission(document, (model,), (ModelUsage("record"),))
        assert report.verified is valid
        assert report.outcome == ("agreement" if valid else "disagreement")


def test_json_contracts_preserve_exact_enum_lexicals_and_decimal_bound_text() -> None:
    data = [
        {
            "identifier": "record",
            "types": [str(EX.Record)],
            "fields": [
                {
                    "name": "score",
                    "predicate": str(EX.score),
                    "values": [
                        {
                            "kind": "literal",
                            "datatype": str(XSD.integer),
                            "constraints": {
                                "enumeration": [
                                    {"lexical": "01", "datatype": str(XSD.integer)}
                                ],
                                "minimum": "1.00",
                                "maximum": "2",
                            },
                        }
                    ],
                }
            ],
        }
    ]
    before = deepcopy(data)
    models = models_from_data(data)
    constraints = models[0].fields[0].values[0].constraints
    assert constraints is not None and isinstance(constraints.enumeration, tuple)
    assert str(constraints.enumeration[0]) == "01" and constraints.minimum == Decimal(
        "1.00"
    )
    document = {
        "@context": {"@vocab": str(EX)},
        "@type": "Record",
        "score": {"@value": "01", "@type": str(XSD.integer)},
    }
    assert check_emission(document, models, (ModelUsage("record"),)).verified
    graph = Graph()
    graph.add((EX.record, RDF.type, EX.Record))
    graph.add((EX.record, EX.score, literal_term("01", datatype=str(XSD.integer))))
    original = set(graph)
    assert check_rdf_emission(
        prepare_source(graph), models, (RdfModelUsage("record", EX.record),)
    ).verified
    assert set(graph) == original
    graph.set((EX.record, EX.score, literal_term("1", datatype=str(XSD.integer))))
    assert (
        check_rdf_emission(
            prepare_source(graph), models, (RdfModelUsage("record", EX.record),)
        ).outcome
        == "disagreement"
    )
    assert data == before
    for invalid in (
        {"minimum": 1.0},
        {"minimum": "NaN"},
        {"minimum": "1_0"},
        {"max_lenght": 2},
        {"enumeration": [{"iri": str(EX.A), "lexical": "x"}]},
    ):
        damaged = deepcopy(data)
        damaged[0]["fields"][0]["values"][0]["constraints"] = invalid  # type: ignore[index]
        with pytest.raises(InputError):
            models_from_data(damaged)


def test_actual_serializer_is_called_once_and_constraint_truth_is_retained() -> None:
    model = _model(
        ValueDeclaration(
            "literal",
            str(XSD.integer),
            constraints=ValueConstraints(minimum=Decimal("1")),
        )
    )
    vocabulary = Graph()
    vocabulary.add((EX.Record, RDF.type, OWL.Class))
    vocabulary.add((EX.score, RDF.type, OWL.DatatypeProperty))
    vocabulary.add((EX.score, RDFS.range, XSD.integer))
    calls: list[tuple[object, str]] = []

    def serialize(instance: object, *, prefix: str) -> dict[str, object]:
        """Produce the ordinary owned JSON document with exact caller options."""
        calls.append((instance, prefix))
        return {"@context": {"@vocab": prefix}, "@type": "Record", "score": instance}

    for number, valid in ((1, True), (0, False)):
        checked = check_instance(
            number,
            serialize,
            (model,),
            (ModelUsage("record"),),
            vocabulary=vocabulary,
            options={"prefix": str(EX)},
        )
        assert checked.report.verified is valid
        assert checked.output["score"] == number  # type: ignore[index]
    assert calls == [(1, str(EX)), (0, str(EX))]
