"""Resource facets and RDF axiom positions remain independently qualified."""

from collections.abc import Iterator, Sequence
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.declarations.resource_roles import (
    ResourceKind,
    ResourceRole,
    RoleBridge,
)
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.resource_roles import RoleUse, check_resource_roles
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.roles import classify_statement
from bijux_ontology.rdf.roles.types import Statement

EX = Namespace("https://example.org/roles/")


def _use(node: URIRef, kind: str, **kwargs: object) -> RoleUse:
    """Anchor the exact selected value, with explicit consumer role choices."""
    return RoleUse(node, ResourceRole(kind, **kwargs), "/value")  # type: ignore[arg-type]


def _vocabulary() -> Graph:
    return (
        Graph()
        .add((EX.C, RDF.type, OWL.Class))
        .add((EX.C, RDF.type, SKOS.Concept))
        .add((EX.C, SKOS.inScheme, EX.S))
        .add((EX.App, RDF.type, OWL.Class))
        .add((EX.Child, RDFS.subClassOf, EX.App))
    )


def test_class_concept_punning_is_explicit_policy_not_profile_validation() -> None:
    vocabulary = _vocabulary()
    for kind in ("class", "concept"):
        allowed = check_resource_roles(vocabulary, (_use(EX.C, kind, punning="allow"),))
        forbidden = check_resource_roles(
            vocabulary, (_use(EX.C, kind, punning="forbid"),)
        )
        assert allowed.verified
        assert forbidden.outcome == "disagreement"
        assert {issue.code for issue in forbidden.issues} == {"roles.punning_forbidden"}
    profile = check_resource_roles(
        vocabulary, (_use(EX.C, "class", punning="allow"),), profile="owl2-dl"
    )
    assert profile.outcome == "unverified" and not profile.verified
    assert "roles.profile_unavailable" in {issue.code for issue in profile.issues}
    assert any(
        child.scope == "resource.profile" and child.requested == 1
        for child in profile.children
    )
    limited_profile = check_resource_roles(
        vocabulary,
        (_use(EX.C, "class"),),
        profile="owl2-dl",
        limits=Limits(max_issues=1),
    )
    assert limited_profile.outcome == "exhausted"
    assert "roles.punning_forbidden" in {issue.code for issue in limited_profile.issues}
    assert sum(issue.kind != "exhausted" for issue in limited_profile.issues) == 1
    # A label and membership alone do not have a Concept domain.
    vocabulary.add((EX.Unclassified, SKOS.inScheme, EX.S)).add(
        (EX.Unclassified, SKOS.prefLabel, Literal("name"))
    )
    assert not check_resource_roles(
        vocabulary, (_use(EX.Unclassified, "concept"),)
    ).verified
    vocabulary.add((EX.Unclassified, SKOS.broader, EX.Other))
    assert check_resource_roles(
        vocabulary, (_use(EX.Unclassified, "concept"),)
    ).verified


def test_instance_requires_actual_type_witness_and_named_mode_is_explicit() -> None:
    vocabulary = _vocabulary()
    required = ResourceRole("instance", types=(str(EX.App),), punning="allow")
    for data in (
        Graph().add((EX.C, RDF.type, OWL.Class)),
        Graph().add((EX.C, RDF.type, SKOS.Concept)),
    ):
        report = check_resource_roles(vocabulary, (RoleUse(EX.C, required),), data=data)
        assert not report.verified and "roles.instance_type_missing" in {
            issue.code for issue in report.issues
        }
    data = Graph().add((EX.C, RDF.type, EX.App))
    assert check_resource_roles(
        vocabulary, (RoleUse(EX.C, required),), data=data
    ).verified
    data = Graph().add((EX.C, RDF.type, EX.Child))
    assert not check_resource_roles(
        vocabulary, (RoleUse(EX.C, required),), data=data
    ).verified
    named = ResourceRole(
        "instance", types=(str(EX.App),), type_mode="named", punning="allow"
    )
    assert check_resource_roles(vocabulary, (RoleUse(EX.C, named),), data=data).verified
    vocabulary.add((EX.Child, OWL.disjointWith, EX.App))
    conflict = check_resource_roles(vocabulary, (RoleUse(EX.C, named),), data=data)
    assert "roles.type_conflict" in {issue.code for issue in conflict.issues}
    # A domain consequence is not an asserted instance-type witness.
    vocabulary = _vocabulary().add((EX.p, RDFS.domain, EX.App))
    data = Graph().add((EX.Instance, EX.p, EX.Value))
    assert not check_resource_roles(
        vocabulary, (RoleUse(EX.Instance, required),), data=data
    ).verified


def test_focus_bridge_keeps_separate_identifiers_and_instance_assertions() -> None:
    vocabulary = (
        _vocabulary()
        .add((EX.Concept, RDF.type, SKOS.Concept))
        .add((EX.Concept, EX.focus, EX.Class))
        .add((EX.Class, RDF.type, OWL.Class))
        .add((EX.Class, RDF.type, EX.App))
    )
    bridge = ResourceRole("class", bridge=RoleBridge(str(EX.focus)), punning="allow")
    result = check_resource_roles(vocabulary, (RoleUse(EX.Concept, bridge),))
    assert result.verified
    assert (EX.Concept, EX.focus, EX.Class) in vocabulary
    assert not check_resource_roles(vocabulary, (_use(EX.Concept, "class"),)).verified
    required = ResourceRole("instance", types=(str(EX.App),))
    assert not check_resource_roles(
        vocabulary, (RoleUse(EX.Concept, required),)
    ).verified
    vocabulary.add((EX.Concept, EX.focus, EX.OtherClass)).add(
        (EX.OtherClass, RDF.type, OWL.Class)
    )
    assert not check_resource_roles(vocabulary, (RoleUse(EX.Concept, bridge),)).verified
    reverse = (
        Graph()
        .add((EX.Concept, RDF.type, SKOS.Concept))
        .add((EX.Class, RDF.type, OWL.Class))
        .add((EX.Class, EX.focus, EX.Concept))
    )
    assert not check_resource_roles(reverse, (RoleUse(EX.Concept, bridge),)).verified
    reverse_bridge = ResourceRole(
        "class", bridge=RoleBridge(str(EX.focus), reverse=True)
    )
    assert check_resource_roles(
        reverse, (RoleUse(EX.Concept, reverse_bridge),)
    ).verified


def test_per_term_occurrences_distinguish_punning_and_declaration_positions() -> None:
    def roles(statement: Statement) -> set[tuple[str, str, object]]:
        return {
            (item.position, item.role, item.term)
            for item in classify_statement(
                statement, source_scope="local", graph=EX.graph
            )
        }

    self_type = roles((EX.C, RDF.type, EX.C))
    assert ("subject", "individual", EX.C) in self_type
    assert ("object", "class", EX.C) in self_type
    declaration = roles((EX.C, RDF.type, OWL.Class))
    assert ("subject", "class", EX.C) in declaration
    assert ("object", "declaration", OWL.Class) in declaration
    assert not any(
        position == "subject" and role == "individual"
        for position, role, _ in declaration
    )
    scheme = roles((EX.C, SKOS.inScheme, EX.S))
    assert ("subject", "individual", EX.C) in scheme
    assert ("subject", "class", EX.C) not in scheme
    assert ("subject", "class", EX.C) in roles((EX.C, RDFS.subClassOf, EX.App))
    custom = classify_statement((EX.C, EX.custom, EX.Other))
    assert {item.role for item in custom if item.position in ("subject", "object")} == {
        "unknown"
    }
    explicit = classify_statement(
        (EX.C, EX.custom, EX.Other), subject_role="individual", object_role="class"
    )
    assert any(item.position == "object" and item.role == "class" for item in explicit)
    with pytest.raises(InputError):
        classify_statement((EX.C, RDFS.subClassOf, EX.App), subject_role="individual")
    # Equal-form literals and SKOS mapping never become class/equality axioms.
    assert ("subject", "class", EX.C) not in roles((EX.C, SKOS.broader, EX.App))
    assert ("object", "individual", EX.Other) in roles(
        (EX.C, SKOS.exactMatch, EX.Other)
    )


def test_exact_selected_graph_keeps_instance_witnesses_and_owned_blank_identity() -> (
    None
):
    data = Dataset()
    data.graph(EX.one).add((EX.Instance, RDF.type, EX.App))
    data.graph(EX.two).add((EX.Instance, RDF.type, SKOS.Concept))
    source = prepare_rdf(data)
    required = (RoleUse(EX.Instance, ResourceRole("instance", types=(str(EX.App),))),)
    assert check_resource_roles(
        _vocabulary(), required, data=source, graph=EX.one
    ).verified
    assert not check_resource_roles(
        _vocabulary(), required, data=source, graph=EX.two
    ).verified
    with pytest.raises(InputError):
        check_resource_roles(_vocabulary(), required, data=source)
    blank_source = prepare_rdf(Graph().add((BNode(), RDF.type, EX.App)))
    blank = next(blank_source.graph().subjects(RDF.type, EX.App))
    assert isinstance(blank, BNode)
    assert check_resource_roles(
        _vocabulary(),
        (RoleUse(blank, ResourceRole("instance", types=(str(EX.App),))),),
        data=blank_source,
    ).verified


def test_bounded_capture_metadata_and_prior_findings_remain_explicit() -> None:
    class Indexed(Sequence[RoleUse]):
        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int) -> RoleUse:  # type: ignore[override]
            if index != 0:
                raise IndexError(index)
            return _use(EX.C, "class", punning="allow")

        def __iter__(self) -> Iterator[RoleUse]:
            raise AssertionError("Bounded sequence capture must use indices")

    assert check_resource_roles(_vocabulary(), Indexed()).verified
    huge = "x" * 100_000
    use = RoleUse(EX.C, ResourceRole("class"), huge)
    report = check_resource_roles(_vocabulary(), (use,), limits=Limits(max_bytes=100))
    assert report.outcome == "exhausted"
    assert all(issue.location != huge for issue in report.issues)
    with pytest.raises(InputError):
        ResourceRole("class", types=(str(EX.App),))
    with pytest.raises(InputError):
        RoleBridge("relative")
    with pytest.raises(InputError):
        RoleUse(cast(URIRef, "https://example.org/resource"), ResourceRole("class"))
    retained = check_resource_roles(
        _vocabulary(),
        (
            RoleUse(EX.C, ResourceRole("class"), "/first"),
            RoleUse(EX.C, ResourceRole("class"), "/second"),
        ),
        limits=Limits(max_issues=1),
    )
    assert retained.outcome == "exhausted"
    assert any(
        issue.code == "roles.punning_forbidden" and issue.location == "/first"
        for issue in retained.issues
    )
    for work in range(1, 100):
        report = check_resource_roles(
            _vocabulary(), (_use(EX.C, "class"),), budget=Budget(Limits(max_work=work))
        )
        assert not report.verified


def test_actual_fields_native_and_once_serializer_keep_alternatives_local() -> None:
    from typing import Any, ClassVar

    from pydantic import BaseModel, Field

    from bijux_ontology.declarations.types import (
        FieldDeclaration,
        ModelDeclaration,
        ValueDeclaration,
    )
    from bijux_ontology.models.emission import ModelUsage, check_emission
    from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission
    from bijux_ontology.models.serialization import check_instance

    class Reference(BaseModel):
        identifier: str = Field(serialization_alias="@id")
        kind: str = Field(serialization_alias="@type")

    class Record(BaseModel):
        calls: ClassVar[list[dict[str, object]]] = []
        kind: str = Field(serialization_alias="@type")
        value: Reference | str

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            type(self).calls.append(kwargs)
            return super().model_dump(**kwargs)

    vocabulary = (
        _vocabulary()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.ObjectProperty))
        .add((EX.ClassOnly, RDF.type, OWL.Class))
    )
    context = {"value": {"@id": str(EX.value), "@type": "@id"}}
    instance = ValueDeclaration(
        "object", types=(str(EX.App),), role=ResourceRole("instance", punning="allow")
    )
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (instance,)),),
    )
    options: dict[str, object] = {
        "mode": "json",
        "by_alias": True,
        "exclude_none": True,
    }
    for kind, expected in ((EX.App, True), (SKOS.Concept, False), (OWL.Class, False)):
        Record.calls.clear()
        result = check_instance(
            Record(
                kind=str(EX.Record),
                value=Reference(identifier=str(EX.C), kind=str(kind)),
            ),
            Record.model_dump,
            (model,),
            (ModelUsage("record"),),
            vocabulary=vocabulary,
            context=context,
            options=options,
        )
        assert result.report.verified is expected
        assert Record.calls == [options]
        assert result.output == {
            "@type": str(EX.Record),
            "value": {"@id": str(EX.C), "@type": str(kind)},
        }
        graph = prepare_rdf(
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.value, EX.C))
            .add((EX.C, RDF.type, kind))
        )
        native = check_rdf_emission(
            graph,
            (model,),
            (RdfModelUsage("record", EX.record),),
            vocabulary=vocabulary,
        )
        assert native.verified is expected
    # An actual named subclass witness works without duplicated authored types.
    named = ValueDeclaration(
        "object",
        types=(str(EX.App),),
        role=ResourceRole("instance", type_mode="named", punning="allow"),
    )
    named_model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (named,)),),
    )
    assert check_instance(
        Record(
            kind=str(EX.Record),
            value=Reference(identifier=str(EX.C), kind=str(EX.Child)),
        ),
        Record.model_dump,
        (named_model,),
        (ModelUsage("record"),),
        vocabulary=vocabulary,
        context=context,
        options=options,
    ).report.verified
    # The rejected instance alternative must not invalidate a separate class choice.
    alternatives = (
        instance,
        ValueDeclaration("iri", role=ResourceRole("class", punning="allow")),
    )
    union_model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), alternatives),),
    )
    source = {"@context": context, "@type": str(EX.Record), "value": str(EX.ClassOnly)}
    assert check_emission(
        source, (union_model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified
    assert not check_emission(source, (union_model,), (ModelUsage("record"),)).verified
    data = prepare_rdf(
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add((EX.record, EX.value, EX.ClassOnly))
    )
    assert check_rdf_emission(
        data,
        (union_model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    unknown_model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (
                    ValueDeclaration("iri", role=ResourceRole("concept")),
                    alternatives[1],
                ),
            ),
        ),
    )
    assert check_emission(
        source, (unknown_model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified
    assert check_rdf_emission(
        data,
        (unknown_model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    # Without an accepted alternative, role declaration and mapping mutations fail.
    class_model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (alternatives[1],)),),
    )
    forbidden_model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (
            FieldDeclaration(
                "value",
                str(EX.value),
                (ValueDeclaration("iri", role=ResourceRole("class")),),
            ),
        ),
    )
    assert not check_emission(
        source, (forbidden_model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified
    assert not check_rdf_emission(
        data,
        (forbidden_model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    missing = (
        _vocabulary()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.ObjectProperty))
    )
    assert not check_emission(
        source, (class_model,), (ModelUsage("record"),), vocabulary=missing
    ).verified
    mutated = {**source, "@context": {"value": str(EX.value)}}
    assert not check_emission(
        mutated, (class_model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified


def test_strict_role_ingestion_and_source_key_requirements_remain_separate() -> None:
    from bijux_ontology.declarations.codecs import models_from_data

    data: list[dict[str, object]] = [
        {
            "identifier": "record",
            "types": [str(EX.Record)],
            "fields": [
                {
                    "name": "value",
                    "predicate": str(EX.value),
                    "values": [
                        {
                            "kind": "iri",
                            "role": {
                                "kind": "class",
                                "punning": "allow",
                                "bridge": {
                                    "predicate": str(EX.focus),
                                    "source_role": "concept",
                                },
                            },
                        }
                    ],
                }
            ],
        }
    ]
    model = models_from_data(data)[0]
    role = model.fields[0].values[0].role
    assert (
        role is not None
        and role.kind == "class"
        and role.bridge == RoleBridge(str(EX.focus))
    )
    for extra in (
        {"kind": "instance", "unknown": True},
        {"kind": "class", "types": [str(EX.App)]},
        {"kind": "class", "bridge": {"predicate": str(EX.focus), "guess": True}},
    ):
        invalid = [
            {
                "identifier": "record",
                "types": [],
                "fields": [
                    {
                        "name": "value",
                        "predicate": str(EX.value),
                        "values": [{"kind": "iri", "role": extra}],
                    }
                ],
            }
        ]
        with pytest.raises(InputError):
            models_from_data(invalid)


def test_actual_bridge_preserves_independent_source_type_obligations() -> None:
    from bijux_ontology.declarations.types import (
        FieldDeclaration,
        ModelDeclaration,
        ValueDeclaration,
    )
    from bijux_ontology.models.emission import ModelUsage, check_emission
    from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission

    vocabulary = (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.ObjectProperty))
        .add((EX.App, RDF.type, OWL.Class))
        .add((EX.Concept, RDF.type, SKOS.Concept))
        .add((EX.Class, RDF.type, OWL.Class))
        .add((EX.Concept, EX.focus, EX.Class))
    )
    source = {
        "@context": {"value": {"@id": str(EX.value), "@type": "@id"}},
        "@type": str(EX.Record),
        "value": str(EX.Concept),
    }
    bridge = RoleBridge(str(EX.focus))
    class_value = ValueDeclaration(
        "iri", role=ResourceRole("class", bridge=bridge, punning="allow")
    )
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (class_value,)),),
    )
    assert check_emission(
        source, (model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified
    graph = (
        Graph()
        .add((EX.record, RDF.type, EX.Record))
        .add((EX.record, EX.value, EX.Concept))
        .add((EX.Concept, RDF.type, SKOS.Concept))
        .add((EX.Class, RDF.type, EX.App))
    )
    assert check_rdf_emission(
        prepare_rdf(graph),
        (model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    required = ResourceRole(
        "instance", types=(str(EX.App),), bridge=bridge, punning="allow"
    )
    constrained = ValueDeclaration("object", types=(str(EX.App),), role=required)
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (constrained,)),),
    )
    # Target Class's App type cannot discharge source Concept's own App type.
    assert not check_rdf_emission(
        prepare_rdf(graph),
        (model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    graph.add((EX.Concept, RDF.type, EX.App))
    assert check_rdf_emission(
        prepare_rdf(graph),
        (model,),
        (RdfModelUsage("record", EX.record),),
        vocabulary=vocabulary,
    ).verified
    # Source mode has no established target actual-graph typing and stays unknown.
    assert not check_emission(
        source, (model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified


def test_actual_type_positions_supply_source_facets_without_inventing_instances() -> (
    None
):
    from bijux_ontology.declarations.types import (
        FieldDeclaration,
        ModelDeclaration,
        ValueDeclaration,
    )
    from bijux_ontology.models.emission import ModelUsage, check_emission
    from bijux_ontology.models.rdf_emission import RdfModelUsage, check_rdf_emission

    vocabulary = (
        Graph()
        .add((EX.Record, RDF.type, OWL.Class))
        .add((EX.value, RDF.type, OWL.ObjectProperty))
        .add((EX.App, RDF.type, OWL.Class))
    )
    cases: tuple[tuple[URIRef, ResourceKind], ...] = (
        (SKOS.Concept, "concept"),
        (OWL.Class, "class"),
        (EX.C, "class"),
    )
    for kind, role in cases:
        value = ValueDeclaration("object", role=ResourceRole(role, punning="allow"))
        model = ModelDeclaration(
            "record",
            (str(EX.Record),),
            (FieldDeclaration("value", str(EX.value), (value,)),),
        )
        document = {
            "@context": {"value": str(EX.value)},
            "@type": str(EX.Record),
            "value": {"@id": str(EX.C), "@type": str(kind)},
        }
        assert check_emission(
            document, (model,), (ModelUsage("record"),), vocabulary=vocabulary
        ).verified
        graph = prepare_rdf(
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.value, EX.C))
            .add((EX.C, RDF.type, kind))
        )
        assert check_rdf_emission(
            graph,
            (model,),
            (RdfModelUsage("record", EX.record),),
            vocabulary=vocabulary,
        ).verified
    # Declaration metadata alone still cannot establish the requested App instance.
    value = ValueDeclaration(
        "object", types=(str(EX.App),), role=ResourceRole("instance", punning="allow")
    )
    model = ModelDeclaration(
        "record",
        (str(EX.Record),),
        (FieldDeclaration("value", str(EX.value), (value,)),),
    )
    assert not check_emission(
        document, (model,), (ModelUsage("record"),), vocabulary=vocabulary
    ).verified
