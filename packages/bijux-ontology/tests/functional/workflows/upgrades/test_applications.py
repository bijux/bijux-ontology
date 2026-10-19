"""Independent release declarations and real selected serializer evidence."""

import importlib.util
from dataclasses import dataclass, replace

import pytest
from rdflib import BNode, Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.declarations.resource_roles import ResourceRole
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.corpus.checking import SerializerCase
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.vocabulary.facts.types import FactPredicate, FactSelection
from bijux_ontology.workflows.upgrades.applications import (
    ApplicationField,
    ApplicationOutput,
    ApplicationVersion,
    check_application,
    compare_applications,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("pyld") is None, reason="requires JSON-LD extra"
)
E = Namespace("urn:application-upgrade:")


def _schema() -> Graph:
    """Declare independent predicate choices, neither privileged by this workflow."""
    return (
        Graph()
        .add((E.Record, RDF.type, OWL.Class))
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.q, RDF.type, OWL.DatatypeProperty))
    )


def _model(
    predicate: str,
    *,
    identifier: str = "Record",
    field: str = "value",
    key: str = "value",
) -> ModelDeclaration:
    """Define an actual side-specific model name and emitted spelling."""
    return ModelDeclaration(
        identifier,
        (str(E.Record),),
        (
            FieldDeclaration(
                field,
                predicate,
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
                key=key,
            ),
        ),
    )


def _version(
    predicate: str = str(E.p),
    *,
    text: str = "one",
    identifier: str = "Record",
    field: str = "value",
    key: str = "value",
) -> ApplicationVersion:
    """Bind stable consumer IDs explicitly across actual declaration renames."""
    return ApplicationVersion(
        models={
            "record": _model(predicate, identifier=identifier, field=field, key=key)
        },
        fields={"title": ApplicationField(identifier, field, {"sample": (key,)})},
        outputs={
            "sample": ApplicationOutput(
                {"@id": str(E.item), "@type": str(E.Record), key: text},
                (ModelUsage(identifier),),
            )
        },
        context={key: predicate},
    )


def test_actual_serializer_once_with_owned_options_and_resources() -> None:
    @dataclass
    class Record:
        value: str

    calls: list[tuple[object, bool]] = []

    def emit(instance: object, *, aliases: bool) -> object:
        assert isinstance(instance, Record)
        calls.append((instance, aliases))
        return {"@id": str(E.item), "@type": str(E.Record), "value": instance.value}

    options: dict[str, object] = {"aliases": True}
    context = {"value": str(E.p)}
    url = "https://example.test/context"
    resources = {url: {"@context": context}}
    version = ApplicationVersion(
        models={"record": _model(str(E.p))},
        fields={"title": ApplicationField("Record", "value", {"sample": ("value",)})},
        cases=(
            SerializerCase(
                "sample", Record("one"), emit, (ModelUsage("Record"),), options=options
            ),
        ),
        context=url,
        resources=resources,
    )
    resources.clear()
    options["aliases"] = False
    context["value"] = str(E.q)
    located = ApplicationVersion(
        models={
            "record": replace(
                version.models["record"],
                location="after.json#/models/record",
                fields=tuple(
                    replace(field, location=f"after.json#/models/record/fields/{index}")
                    for index, field in enumerate(version.models["record"].fields)
                ),
            )
        },
        fields=version.fields,
        cases=version.cases,
        context=version.context,
        resources=version.resources,
    )
    result = compare_applications(_schema(), _schema(), before=version, after=located)
    assert calls == [(version.cases[0].instance, True)] * 2
    assert result.before.verified and result.after.verified and result.report.verified
    assert not result.declarations and not result.samples[0].values
    assert result.samples[0].dataset.verified and result.samples[0].mapping.verified
    calls.clear()
    single = check_application(_schema(), version)
    assert single.report.verified
    assert calls == [(version.cases[0].instance, True)]
    assert result.samples[0].before is not None
    assert single.samples["sample"].document == result.samples[0].before.document


def test_both_valid_predicate_and_field_name_changes_are_explicit() -> None:
    url = "https://example.test/versioned-context"
    old = _version()
    new = _version(str(E.q), identifier="Renamed", field="label", key="title")

    def external(version: ApplicationVersion) -> ApplicationVersion:
        return ApplicationVersion(
            models=version.models,
            fields=version.fields,
            outputs=version.outputs,
            context=url,
            resources={url: {"@context": version.context}},
        )

    result = compare_applications(
        _schema(), _schema(), before=external(old), after=external(new)
    )
    assert result.before.verified and result.after.verified
    assert any(
        delta.consumer == "title" and "predicate" in delta.aspects
        for delta in result.declarations
    )
    assert result.samples[0].dataset.outcome == "disagreement"
    old_usage = result.samples[0].observations[0][1]
    new_usage = result.samples[0].observations[0][2]
    assert old_usage is not None and old_usage.path == ("value",)
    assert new_usage is not None and new_usage.path == ("title",)
    assert not result.demonstrated_break


def test_coordinated_fix_and_missing_baseline_have_distinct_status() -> None:
    result = compare_applications(
        _schema(), _schema(), before=_version(str(E.missing)), after=_version()
    )
    assert not result.before.verified and result.candidate_report.verified
    assert any(change.status == "resolved" for change in result.contracts)
    assert not result.demonstrated_break
    base = _version()
    broken = ApplicationVersion(
        models=base.models,
        fields=base.fields,
        context=base.context,
        outputs={
            "sample": ApplicationOutput(
                {"@id": str(E.item), "@type": str(E.Record), "value": 1},
                (ModelUsage("Record"),),
            )
        },
    )
    regression = compare_applications(_schema(), _schema(), before=base, after=broken)
    assert regression.demonstrated_break
    assert any(
        change.status == "introduced" and change.contract == "sample/sample"
        for change in regression.contracts
    )
    missing = compare_applications(_schema(), _schema(), after=_version())
    assert missing.candidate_report.verified and not missing.report.complete
    assert not any(change.status == "introduced" for change in missing.contracts)
    assert not missing.demonstrated_break


def test_actual_literal_and_joint_blank_topology_changes() -> None:
    literal = compare_applications(
        _schema(), _schema(), before=_version(), after=_version(text="two")
    )
    assert literal.before.verified and literal.after.verified
    assert literal.samples[0].values[0].consumer == "title"
    assert literal.samples[0].dataset.outcome == "disagreement"
    assert literal.samples[0].mapping.verified
    graph = _schema()
    graph.add((E.left, RDF.type, OWL.ObjectProperty)).add(
        (E.right, RDF.type, OWL.ObjectProperty)
    )
    model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        tuple(
            FieldDeclaration(name, str(E[name]), (ValueDeclaration("object"),))
            for name in ("left", "right")
        ),
    )

    def version(right: str) -> ApplicationVersion:
        return ApplicationVersion(
            models={"record": model},
            fields={
                name: ApplicationField("Record", name, {"sample": (name,)})
                for name in ("left", "right")
            },
            outputs={
                "sample": ApplicationOutput(
                    {
                        "@id": str(E.item),
                        "@type": str(E.Record),
                        "left": {"@id": "_:a"},
                        "right": {"@id": right},
                    },
                    (ModelUsage("Record"),),
                )
            },
            context={"left": str(E.left), "right": str(E.right)},
        )

    topology = compare_applications(
        graph, graph, before=version("_:a"), after=version("_:b")
    )
    assert topology.before.verified and topology.after.verified
    assert topology.samples[0].dataset.outcome == "disagreement"
    assert topology.samples[0].mapping.outcome == "disagreement"
    assert not topology.samples[0].values
    list_model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        (
            FieldDeclaration(
                "value",
                str(E.p),
                (
                    ValueDeclaration(
                        "literal", datatype=str(XSD.string), container="list"
                    ),
                ),
            ),
        ),
    )

    def lists(groups: tuple[tuple[str, ...], ...]) -> ApplicationVersion:
        return ApplicationVersion(
            models={"record": list_model},
            fields={
                "title": ApplicationField("Record", "value", {"sample": ("value",)})
            },
            outputs={
                "sample": ApplicationOutput(
                    {
                        "@id": str(E.item),
                        "@type": str(E.Record),
                        "value": [{"@list": list(group)} for group in groups],
                    },
                    (ModelUsage("Record"),),
                )
            },
            context={"value": str(E.p)},
        )

    groups = lists((("a", "b"), ("c", "d")))
    reordered = compare_applications(
        _schema(), _schema(), before=groups, after=lists((("c", "d"), ("a", "b")))
    )
    assert (
        reordered.samples[0].dataset.verified and reordered.samples[0].mapping.verified
    )
    assert not reordered.samples[0].values
    assert any(
        issue.code == "models.container_unverified" for issue in reordered.before.issues
    )
    regrouped = compare_applications(
        _schema(), _schema(), before=groups, after=lists((("a", "c"), ("b", "d")))
    )
    assert (
        regrouped.samples[0].values
        and regrouped.samples[0].dataset.outcome == "disagreement"
    )


def test_selected_punned_class_change_does_not_invent_concept_impact() -> None:
    old = (
        _schema()
        .add((E.C, RDF.type, OWL.Class))
        .add((E.C, RDF.type, SKOS.Concept))
        .add((E.Parent, RDF.type, OWL.Class))
    )
    new = Graph() + old
    new.add((E.C, RDFS.subClassOf, E.Parent))
    base = _version()
    version = ApplicationVersion(
        models=base.models,
        fields=base.fields,
        outputs=base.outputs,
        context=base.context,
        role_uses={
            "concept": RoleUse(
                E.C, ResourceRole("concept", punning="allow"), "selected-concept"
            )
        },
    )
    result = compare_applications(
        old,
        new,
        before=version,
        after=version,
        fact_selection=FactSelection(
            (FactPredicate(str(RDFS.subClassOf), subject_role="class"),)
        ),
    )
    assert result.before.verified and result.after.verified
    assert not result.before_fact_uses and not result.after_fact_uses
    assert not any(item.use.role == "concept" for item in result.before_affected)
    object_schema = Graph() + old
    object_schema.remove((E.p, RDF.type, OWL.DatatypeProperty))
    object_schema.add((E.p, RDF.type, OWL.ObjectProperty))
    role = ResourceRole("concept", punning="allow")
    role_model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        (FieldDeclaration("value", str(E.p), (ValueDeclaration("iri", role=role),)),),
    )

    def role_version(explicit: bool) -> ApplicationVersion:
        return ApplicationVersion(
            models={"record": role_model},
            fields=base.fields,
            context=base.context,
            outputs={
                "sample": ApplicationOutput(
                    {
                        "@id": str(E.item),
                        "@type": str(E.Record),
                        "value": {"@id": str(E.C)},
                    },
                    (ModelUsage("Record"),),
                )
            },
            role_uses={"concept": RoleUse(E.C, role, "selected-concept")}
            if explicit
            else {},
        )

    attributed = role_version(True)
    assert compare_applications(
        object_schema, object_schema, before=attributed, after=attributed
    ).report.verified
    unbound = role_version(False)
    assert not compare_applications(
        object_schema, object_schema, before=unbound, after=unbound
    ).report.verified
    mixed_schema = Graph() + object_schema
    mixed_schema.remove((E.p, RDF.type, OWL.ObjectProperty))
    mixed_schema.add((E.p, RDF.type, RDF.Property))
    mixed_model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        (
            FieldDeclaration(
                "value",
                str(E.p),
                (
                    ValueDeclaration("literal", datatype=str(XSD.string)),
                    ValueDeclaration("iri", role=role),
                ),
            ),
        ),
    )
    mixed = ApplicationVersion(
        models={"record": mixed_model},
        fields=base.fields,
        outputs=base.outputs,
        context=base.context,
    )
    assert compare_applications(
        mixed_schema, mixed_schema, before=mixed, after=mixed
    ).report.verified


def test_unexercised_required_fields_and_small_budgets_never_green() -> None:
    base = _version()
    unexercised = ApplicationVersion(
        models=base.models,
        fields={"title": ApplicationField("Record", "value")},
        outputs=base.outputs,
        context=base.context,
    )
    result = compare_applications(
        _schema(), _schema(), before=unexercised, after=unexercised
    )
    assert not result.report.verified
    assert any(i.kind == "unverified" for i in result.report.issues)
    wrong_path = ApplicationVersion(
        models=base.models,
        fields={"title": ApplicationField("Record", "value", {"sample": ("other",)})},
        outputs={
            "sample": ApplicationOutput(
                {
                    "@id": str(E.item),
                    "@type": str(E.Record),
                    "value": "one",
                    "other": "two",
                },
                (ModelUsage("Record"),),
            )
        },
        context={"value": str(E.p), "other": str(E.q)},
    )
    wrong = compare_applications(
        _schema(), _schema(), before=wrong_path, after=wrong_path
    )
    assert wrong.before.verified and not wrong.report.verified
    assert not wrong.samples[0].mapping.complete
    alias_model = ModelDeclaration(
        "Record",
        (str(E.Record),),
        (
            FieldDeclaration(
                "value",
                str(E.p),
                (ValueDeclaration("literal", datatype=str(XSD.string)),),
            ),
            FieldDeclaration(
                "other",
                str(E.p),
                (ValueDeclaration("literal", datatype=str(XSD.integer)),),
            ),
        ),
    )
    aliased = ApplicationVersion(
        models={"record": alias_model},
        fields={"title": ApplicationField("Record", "value", {"sample": ("other",)})},
        outputs={
            "sample": ApplicationOutput(
                {
                    "@id": str(E.item),
                    "@type": str(E.Record),
                    "value": "one",
                    "other": 2,
                },
                (ModelUsage("Record"),),
            )
        },
        context={"value": str(E.p), "other": str(E.p)},
    )
    alias = compare_applications(_schema(), _schema(), before=aliased, after=aliased)
    assert alias.before.verified and not alias.samples[0].mapping.complete
    assert not alias.report.verified
    wrong_owner = ApplicationVersion(
        models=base.models,
        fields={
            "title": ApplicationField(
                "Record", "value", {"sample": ("@graph", 1, "value")}
            )
        },
        outputs={
            "sample": ApplicationOutput(
                {
                    "@graph": [
                        {"@id": str(E.item), "@type": str(E.Record), "value": "one"},
                        {"@id": str(E.other), "@type": str(E.Record), "value": "one"},
                    ]
                },
                (ModelUsage("Record", ("@graph", 0)),),
            )
        },
        context=base.context,
    )
    owner = compare_applications(
        _schema(), _schema(), before=wrong_owner, after=wrong_owner
    )
    assert owner.before.verified and not owner.report.verified
    assert not owner.samples[0].mapping.complete
    huge = ApplicationVersion(
        models={"x" * 100000: _model(str(E.p))},
        fields=base.fields,
        outputs=base.outputs,
        context=base.context,
    )
    bounded = compare_applications(
        _schema(), _schema(), before=huge, after=base, limits=Limits(max_bytes=1000)
    )
    assert bounded.report.outcome == "exhausted"
    assert all(len(i.location) <= 1000 for i in bounded.report.issues)
    with pytest.raises(InputError):
        ApplicationOutput({}, "Record")  # type: ignore[arg-type]


def test_anonymous_attachments_keep_independent_consumer_roles_and_scope() -> None:
    def schema(filler: object, owner: object) -> Graph:
        from rdflib.term import Node

        assert isinstance(filler, Node) and isinstance(owner, Node)
        graph = _schema()
        graph.add((E.unused, RDF.type, OWL.ObjectProperty))
        graph.add((E.A, RDF.type, OWL.Class)).add((E.B, RDF.type, OWL.Class))
        node = BNode()
        graph.add((owner, RDFS.range, node)).add((node, OWL.someValuesFrom, filler))
        return graph

    selection = FactSelection(
        (
            FactPredicate(
                str(RDFS.range), subject_role="property", object_role="class"
            ),
            FactPredicate(
                str(OWL.someValuesFrom), subject_role="class", object_role="class"
            ),
        )
    )
    unused = compare_applications(
        schema(E.A, E.unused),
        schema(E.B, E.unused),
        before=_version(),
        after=_version(),
        fact_selection=selection,
    )
    assert unused.report.complete and unused.change_qualification is not None
    assert unused.change_qualification.complete
    assert unused.changes is not None and unused.changes.selected_facts is not None
    assert not unused.changes.selected_facts.report.complete
    assert not unused.before_anonymous_uses and not unused.after_anonymous_uses
    relevant = compare_applications(
        schema(E.A, E.p), schema(E.B, E.p), before=_version(), after=_version(str(E.q))
    )
    assert {
        i.use.field for i in relevant.before_anonymous_uses if i.use.field is not None
    } == {"value"}
    assert not relevant.after_anonymous_uses
    assert relevant.candidate_report.verified


def test_single_application_retains_actual_outputs_without_a_baseline() -> None:
    """The public single-version path shares checks without a synthetic upgrade."""
    version = _version()
    budget = Budget()
    result = check_application(_schema(), version, budget=budget)
    assert result.report.verified
    assert result.version is version
    original = version.outputs["sample"].document
    assert isinstance(original, dict)
    assert result.samples["sample"].document == {
        **original,
        "@context": version.context,
    }
    assert "sample" in result.sources
    assert result.parts["sample/sample"].verified
    assert budget.remaining("work") < budget.limits.max_work
    mismatch = check_application(_schema(), _version(str(E.absent)))
    assert not mismatch.report.verified
    assert mismatch.samples["sample"].document is not None
    assert not check_application(_schema(), ApplicationVersion()).report.verified
    stopped = check_application(_schema(), version, limits=Limits(max_work=1))
    assert stopped.report.outcome == "exhausted"
