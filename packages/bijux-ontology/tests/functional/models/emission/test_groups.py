"""Finite co-emission contracts keep declaration and actual graph scopes aligned."""

from itertools import combinations

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    EmissionCondition,
    EmissionGroup,
    EmissionScenario,
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Limits
from bijux_ontology.models.contracts.checking import check_models
from bijux_ontology.models.emission.checking import check_emission
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("https://example.org/groups/")


def model(
    group: EmissionGroup, *, fields: tuple[str, ...] = ("a", "b", "c", "mode")
) -> ModelDeclaration:
    return ModelDeclaration(
        "record",
        (str(EX.Record),),
        tuple(
            FieldDeclaration(
                name,
                str(EX[name]),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="optional",
            )
            for name in fields
        ),
        emission_groups=(group,),
    )


def vocabulary(*, disjoint: bool = False) -> Graph:
    graph = Graph()
    for kind in (EX.Record, EX.Left, EX.Right):
        graph.add((kind, RDF.type, OWL.Class))
    for name in ("a", "b", "c", "mode"):
        graph.add((EX[name], RDF.type, OWL.DatatypeProperty))
        graph.add((EX[name], RDFS.range, XSD.string))
    if disjoint:
        graph.add((EX.a, RDFS.domain, EX.Left))
        graph.add((EX.b, RDFS.domain, EX.Right))
        graph.add((EX.Left, OWL.disjointWith, EX.Right))
    return graph


def actual(
    declaration: ModelDeclaration, fields: tuple[str, ...], *, mode: object = None
) -> tuple[Report, Report]:
    document: dict[str, object] = {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.record),
        "@type": "Record",
    }
    graph = Graph().add((EX.record, RDF.type, EX.Record))
    for name in fields:
        document[name] = name
        graph.add((EX.record, EX[name], Literal(name)))
    if mode is not None:
        document["mode"] = mode
        values = mode if isinstance(mode, list) else [mode]
        for value in values:
            graph.add((EX.record, EX.mode, Literal(value)))
    jsonld = check_emission(document, (declaration,), (ModelUsage("record"),))
    source = prepare_rdf(RdfSource(graph))
    native = check_rdf_emission(
        source, (declaration,), (RdfModelUsage("record", EX.record),)
    )
    return jsonld, native


def test_exclusion_permits_disjoint_domains_and_checks_both_outputs() -> None:
    declaration = model(
        EmissionGroup("choice", ("a", "b"), maximum=1), fields=("a", "b")
    )
    report = check_models((declaration,), vocabulary(disjoint=True))
    assert report.verified, report.to_dict()
    for fields in (("a",), ("b",), ("a", "b")):
        for result in actual(declaration, fields):
            assert result.verified is (len(fields) == 1)
            if len(fields) == 2:
                assert any(
                    issue.code == "emission.group_count_mismatch"
                    for issue in result.issues
                )


def test_exact_three_field_cases_do_not_reduce_dependency_to_pairs() -> None:
    cases = tuple(
        EmissionScenario("with_" + "_".join(fields) if fields else "empty", fields)
        for fields in (
            fields
            for size in range(4)
            for fields in combinations(("a", "b", "c"), size)
            if fields != ("a", "b")
        )
    )
    declaration = model(
        EmissionGroup("dependency", ("a", "b", "c"), scenarios=cases, closed=True)
    )
    assert check_models((declaration,), vocabulary()).verified
    for fields in (("a", "b"), ("a", "b", "c"), ("a", "c"), ("b", "c")):
        for result in actual(declaration, fields):
            assert result.verified is (fields != ("a", "b"))
    partial = model(EmissionGroup("partial", ("a", "b", "c"), scenarios=cases))
    assert not check_models((partial,), vocabulary()).verified
    for result in actual(partial, ("a", "b")):
        assert not result.verified
        assert any(
            issue.code == "emission.scenario_unverified" and issue.kind == "unverified"
            for issue in result.issues
        )


def test_missing_or_multiple_discriminator_values_remain_unknown() -> None:
    declaration = model(
        EmissionGroup(
            "choice",
            ("a", "b"),
            scenarios=(
                EmissionScenario(
                    "left", ("a",), EmissionCondition("mode", Literal("left"))
                ),
                EmissionScenario(
                    "right", ("b",), EmissionCondition("mode", Literal("right"))
                ),
            ),
            closed=True,
        )
    )
    assert check_models((declaration,), vocabulary()).verified
    for mode in ("left", None, ["left", "right"], "other"):
        for result in actual(declaration, ("a",), mode=mode):
            assert result.verified is (mode == "left")
            if mode is None or isinstance(mode, list):
                assert any(
                    issue.code == "emission.scenario_unverified"
                    for issue in result.issues
                )
            elif mode == "other":
                assert any(
                    issue.code == "emission.scenario_mismatch"
                    for issue in result.issues
                )


def test_impossible_required_groups_and_state_exhaustion_do_not_pass() -> None:
    declaration = model(
        EmissionGroup("exclusive", ("a", "b"), maximum=1), fields=("a", "b")
    )
    fields = tuple(
        FieldDeclaration(
            field.name, field.predicate, field.values, emission="guaranteed"
        )
        for field in declaration.fields
    )
    impossible = ModelDeclaration(
        "record", declaration.types, fields, emission_groups=declaration.emission_groups
    )
    result = check_models((impossible,), vocabulary())
    assert any(
        issue.code == "models.emission_group_conflict" for issue in result.issues
    )
    bounded = check_models((declaration,), vocabulary(), limits=Limits(max_states=1))
    assert not bounded.complete and any(
        issue.kind == "exhausted" for issue in bounded.issues
    )


def test_exactly_one_uses_rdf_emission_and_empty_list_heads_count_once() -> None:
    declaration = model(
        EmissionGroup("one", ("a", "b"), minimum=1, maximum=1), fields=("a", "b")
    )
    for fields in ((), ("a",), ("a", "b")):
        for result in actual(declaration, fields):
            assert result.verified is (len(fields) == 1)
    empty_values: tuple[object, ...] = (None, [])
    for value in empty_values:
        result = check_emission(
            {"@context": {"@vocab": str(EX)}, "@type": "Record", "a": value},
            (declaration,),
            (ModelUsage("record"),),
        )
        assert any(
            issue.code == "emission.group_count_mismatch" for issue in result.issues
        )
    listed = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "a",
                str(EX.a),
                (
                    ValueDeclaration(
                        "literal", datatype=str(XSD.string), container="@list"
                    ),
                ),
            ),
        ),
        emission_groups=(EmissionGroup("one", ("a",), minimum=1, maximum=1),),
    )
    result = check_emission(
        {
            "@context": {
                "@vocab": str(EX),
                "a": {"@id": str(EX.a), "@container": "@list"},
            },
            "@type": "Record",
            "a": [],
        },
        (listed,),
        (ModelUsage("record"),),
    )
    assert result.verified, result.to_dict()
    graph = (
        Graph().add((EX.record, RDF.type, EX.Record)).add((EX.record, EX.a, RDF.nil))
    )
    native = check_rdf_emission(
        prepare_rdf(RdfSource(graph)), (listed,), (RdfModelUsage("record", EX.record),)
    )
    assert native.verified, native.to_dict()


def test_alias_facts_and_split_descriptions_keep_actual_scope() -> None:
    declaration = model(
        EmissionGroup("exclusive", ("a", "b"), maximum=1), fields=("a", "b")
    )
    alias = check_emission(
        {
            "@context": {"@vocab": str(EX), "alias": str(EX.a)},
            "@type": "Record",
            "@id": str(EX.record),
            "alias": "value",
        },
        (declaration,),
        (ModelUsage("record"),),
    )
    assert alias.verified, alias.to_dict()
    document = {
        "@context": {"@vocab": str(EX)},
        "@graph": [
            {"@id": str(EX.record), "@type": "Record", "a": "one"},
            {"@id": str(EX.record), "@type": "Record", "b": "two"},
        ],
    }
    source = check_emission(
        document, (declaration,), (ModelUsage("record", ("@graph", 0)),)
    )
    assert source.verified, source.to_dict()
    joined = check_emission(
        document,
        (declaration,),
        (ModelUsage("record", reference=str(EX.record), graph=None, entity=True),),
    )
    assert any(issue.code == "emission.group_count_mismatch" for issue in joined.issues)
    duplicates = ModelDeclaration(
        "record",
        declaration.types,
        (
            FieldDeclaration(
                "a", str(EX.a), (ValueDeclaration("literal", datatype=str(XSD.string)),)
            ),
            FieldDeclaration(
                "b", str(EX.a), (ValueDeclaration("literal", datatype=str(XSD.string)),)
            ),
        ),
        emission_groups=declaration.emission_groups,
    )
    for result in actual(duplicates, ("a",)):
        assert any(issue.code == "emission.group_unverified" for issue in result.issues)


def test_ingested_discriminator_preserves_exact_literal_and_rejects_bad_fields() -> (
    None
):
    from copy import deepcopy

    import pytest

    from bijux_ontology.declarations.codecs import models_from_data
    from bijux_ontology.diagnostics.errors import InputError

    data: list[dict[str, object]] = [
        {
            "identifier": "record",
            "types": [str(EX.Record)],
            "fields": [
                {
                    "name": name,
                    "predicate": str(EX[name]),
                    "values": [{"kind": "literal", "datatype": str(datatype)}],
                }
                for name, datatype in (("a", XSD.string), ("mode", XSD.integer))
            ],
            "emission_groups": [
                {
                    "identifier": "conditional",
                    "fields": ["a"],
                    "closed": True,
                    "scenarios": [
                        {
                            "identifier": "leading_zero",
                            "present": ["a"],
                            "condition": {
                                "field": "mode",
                                "equals": {
                                    "lexical": "01",
                                    "datatype": str(XSD.integer),
                                },
                            },
                        }
                    ],
                }
            ],
        }
    ]
    declaration = models_from_data(data)[0]
    condition = declaration.emission_groups[0].scenarios[0].condition
    assert condition is not None and str(condition.equals) == "01"
    for lexical in ("01", "1"):
        document = {
            "@context": {"@vocab": str(EX)},
            "@id": str(EX.record),
            "@type": "Record",
            "a": "a",
            "mode": {"@value": lexical, "@type": str(XSD.integer)},
        }
        graph = Graph().add((EX.record, RDF.type, EX.Record))
        graph.add((EX.record, EX.a, Literal("a")))
        graph.add(
            (
                EX.record,
                EX.mode,
                Literal(lexical, datatype=XSD.integer, normalize=False),
            )
        )
        source = check_emission(document, (declaration,), (ModelUsage("record"),))
        native = check_rdf_emission(
            prepare_rdf(RdfSource(graph)),
            (declaration,),
            (RdfModelUsage("record", EX.record),),
        )
        for result in (source, native):
            assert result.verified is (lexical == "01"), result.to_dict()
    for invalid in (["missing"], ["a", "a"]):
        changed = deepcopy(data)
        groups = changed[0]["emission_groups"]
        assert isinstance(groups, list) and isinstance(groups[0], dict)
        groups[0]["fields"] = invalid
        with pytest.raises(InputError):
            models_from_data(changed)


def test_discriminator_alias_and_described_iri_match_native_terms() -> None:
    from dataclasses import replace

    for condition, value, branch in (
        (Literal(1), 1, ValueDeclaration("literal", datatype=str(XSD.integer))),
        (
            EX.Mode,
            {"@id": str(EX.Mode), "description": "mode"},
            ValueDeclaration("object"),
        ),
    ):
        declaration = model(
            EmissionGroup(
                "conditional",
                ("a",),
                scenarios=(
                    EmissionScenario(
                        "with_a", ("a",), EmissionCondition("mode", condition)
                    ),
                ),
                closed=True,
            ),
            fields=("a", "mode"),
        )
        declaration = replace(
            declaration,
            fields=(
                declaration.fields[0],
                replace(declaration.fields[1], values=(branch,)),
            ),
        )
        document = {
            "@context": {"@vocab": str(EX), "disc": str(EX.mode)},
            "@id": str(EX.record),
            "@type": "Record",
            "a": "a",
            "disc": value,
        }
        for binding in (
            ModelUsage("record"),
            ModelUsage("record", reference=str(EX.record), graph=None, entity=True),
        ):
            result = check_emission(document, (declaration,), (binding,))
            assert result.verified, result.to_dict()
        graph = Graph().add((EX.record, RDF.type, EX.Record))
        graph.add((EX.record, EX.a, Literal("a")))
        graph.add((EX.record, EX.mode, condition))
        result = check_rdf_emission(
            prepare_rdf(RdfSource(graph)),
            (declaration,),
            (RdfModelUsage("record", EX.record),),
        )
        assert result.verified, result.to_dict()
    impossible = model(
        EmissionGroup(
            "conditional",
            ("a",),
            scenarios=(
                EmissionScenario("with_a", ("a",), EmissionCondition("mode", EX.Mode)),
            ),
            closed=True,
        )
    )
    result = check_models((impossible,), vocabulary())
    assert any(
        issue.code == "models.scenario_condition_conflict" for issue in result.issues
    )


def test_unrelated_unresolved_observation_retains_established_count_failure() -> None:
    declaration = model(EmissionGroup("choice", ("a", "b"), maximum=1))
    document = {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.record),
        "@type": "Record",
        "a": "a",
        "b": "b",
        "@reverse": {"x": {"@id": str(EX.x)}, "y": {"@id": str(EX.y)}},
    }
    result = check_emission(document, (declaration,), (ModelUsage("record"),))
    assert any(issue.code == "emission.group_count_mismatch" for issue in result.issues)


def test_same_predicate_discriminators_cannot_verify_conflicting_groups() -> None:
    from dataclasses import replace

    declaration = model(
        EmissionGroup(
            "left",
            ("a",),
            scenarios=(
                EmissionScenario(
                    "with_a", ("a",), EmissionCondition("mode", Literal("left"))
                ),
            ),
            closed=True,
        )
    )
    declaration = replace(
        declaration,
        fields=(
            *declaration.fields,
            replace(declaration.fields[-1], name="alias_mode"),
        ),
        emission_groups=(
            *declaration.emission_groups,
            EmissionGroup(
                "right",
                ("b",),
                scenarios=(
                    EmissionScenario(
                        "with_b",
                        ("b",),
                        EmissionCondition("alias_mode", Literal("right")),
                    ),
                ),
                closed=True,
            ),
        ),
    )
    result = check_models((declaration,), vocabulary())
    assert any(
        issue.code == "models.emission_group_unverified" for issue in result.issues
    )
    assert not result.verified

    declaration = replace(
        declaration,
        emission_groups=(
            *declaration.emission_groups,
            EmissionGroup("exclusive", ("a", "b"), maximum=1),
        ),
    )
    for result in actual(declaration, ("a", "b"), mode="left"):
        assert any(
            issue.code == "emission.group_count_mismatch" for issue in result.issues
        )


def test_aliased_group_facts_retain_individual_value_constraints() -> None:
    declaration = model(EmissionGroup("choice", ("a", "b"), minimum=1, maximum=1))
    document = {
        "@context": {"@vocab": str(EX), "alias": str(EX.a)},
        "@id": str(EX.record),
        "@type": "Record",
        "alias": 1,
    }
    source = check_emission(document, (declaration,), (ModelUsage("record"),))
    assert any(issue.code == "emission.value_mismatch" for issue in source.issues)
    assert not source.verified


def test_aliased_child_needs_its_own_established_source_binding() -> None:
    parent = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "child", str(EX.child), (ValueDeclaration("object", model="child"),)
            ),
        ),
        emission_groups=(EmissionGroup("one", ("child",), minimum=1, maximum=1),),
    )
    child = ModelDeclaration(
        "child",
        (str(EX.Child),),
        (
            FieldDeclaration(
                "label",
                str(EX.label),
                (ValueDeclaration("literal"),),
                emission="guaranteed",
            ),
        ),
    )
    inline: dict[str, object] = {"@id": str(EX.kid), "@type": "Child", "label": "Name"}
    document = {
        "@context": {"@vocab": str(EX), "alias": str(EX.child)},
        "@id": str(EX.record),
        "@type": "Record",
        "alias": inline,
    }
    models = (parent, child)
    unbound = check_emission(document, models, (ModelUsage("record"),))
    assert not unbound.verified and any(
        issue.code == "emission.nested_scope_unverified" for issue in unbound.issues
    )
    usages = (ModelUsage("record"), ModelUsage("child", ("alias",)))
    bound = check_emission(document, models, usages)
    assert bound.verified, bound.to_dict()
    inline.pop("label")
    missing = check_emission(document, models, usages)
    assert not missing.verified and any(
        issue.code == "emission.required_occurrence_missing" for issue in missing.issues
    )


def test_shared_predicate_across_presence_groups_is_explicitly_unverified() -> None:
    declaration = ModelDeclaration(
        "record",
        (str(EX.Record),),
        tuple(
            FieldDeclaration(
                name,
                str(EX.a),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
            )
            for name in ("a", "b")
        ),
        emission_groups=(
            EmissionGroup("absent", ("a",), maximum=0),
            EmissionGroup("present", ("b",), minimum=1, maximum=1),
        ),
    )
    report = check_models((declaration,), vocabulary())
    assert not report.verified and any(
        issue.code == "models.emission_group_unverified" for issue in report.issues
    )


def test_authored_literal_condition_needs_supported_valid_lexical_form() -> None:
    from dataclasses import replace

    for datatype, lexical, code in (
        (XSD.integer, "not-an-integer", "literal.lexical_invalid"),
        (EX.custom, "value", "literal.datatype_unverified"),
    ):
        declaration = model(
            EmissionGroup(
                "conditional",
                ("a",),
                scenarios=(
                    EmissionScenario(
                        "with_a",
                        ("a",),
                        EmissionCondition(
                            "mode", Literal(lexical, datatype=datatype, normalize=False)
                        ),
                    ),
                ),
                closed=True,
            )
        )
        declaration = replace(
            declaration,
            fields=(
                *declaration.fields[:-1],
                replace(
                    declaration.fields[-1],
                    values=(ValueDeclaration("literal", datatype=str(datatype)),),
                ),
            ),
        )
        graph = vocabulary()
        graph.remove((EX.mode, RDFS.range, None))
        graph.add((EX.mode, RDFS.range, datatype))
        report = check_models((declaration,), graph)
        assert not report.verified and any(
            issue.code == code for issue in report.issues
        ), report.to_dict()


def test_scenario_discriminator_satisfies_its_application_value_contract() -> None:
    from dataclasses import replace

    for constraints in (
        ValueConstraints(enumeration=(Literal("allowed"),)),
        ValueConstraints(pattern="allowed"),
    ):
        for lexical in ("allowed", "forbidden"):
            declaration = model(
                EmissionGroup(
                    "conditional",
                    ("mode",),
                    minimum=1,
                    maximum=1,
                    scenarios=(
                        EmissionScenario(
                            "selected",
                            ("mode",),
                            EmissionCondition("mode", Literal(lexical)),
                        ),
                    ),
                    closed=True,
                ),
                fields=("mode",),
            )
            declaration = replace(
                declaration,
                fields=(
                    replace(
                        declaration.fields[0],
                        values=(
                            ValueDeclaration(
                                "literal",
                                datatype=str(XSD.string),
                                constraints=constraints,
                            ),
                        ),
                    ),
                ),
            )
            result = check_models((declaration,), vocabulary())
            assert result.verified is (lexical == "allowed"), result.to_dict()
            if lexical == "forbidden":
                assert any(
                    issue.code == "models.scenario_condition_conflict"
                    for issue in result.issues
                )
            for emitted in actual(declaration, (), mode=lexical):
                assert emitted.verified is (lexical == "allowed"), emitted.to_dict()


def test_guaranteed_emission_cannot_forbid_every_rdf_value() -> None:
    import pytest

    from bijux_ontology.declarations.types import OutputRequirement
    from bijux_ontology.diagnostics.errors import InputError

    output = OutputRequirement(maximum_values=0, allow_empty=True, allow_null=True)
    with pytest.raises(InputError, match="Guaranteed emission"):
        FieldDeclaration(
            "mode",
            str(EX.mode),
            (ValueDeclaration("literal", datatype=str(XSD.string)),),
            emission="guaranteed",
            output=output,
        )
    declaration = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "mode",
                str(EX.mode),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                emission="optional",
                output=output,
            ),
        ),
    )
    assert check_models((declaration,), vocabulary()).verified
    for emitted in actual(declaration, ()):
        assert emitted.verified, emitted.to_dict()
