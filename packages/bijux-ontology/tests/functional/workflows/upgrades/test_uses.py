"""Use evidence retains application ownership and actual processor meanings."""

from rdflib.namespace import XSD

from bijux_ontology.declarations import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.limits import Limits
from bijux_ontology.workflows.upgrades.uses import index_uses

EX = "https://example.org/"


def test_recursive_child_types_are_indexed_at_each_referring_field() -> None:
    parent = ModelDeclaration(
        "Parent",
        (EX + "Parent",),
        (
            FieldDeclaration(
                "child",
                EX + "child",
                (ValueDeclaration("object", model="Child"),),
                reverse=True,
            ),
        ),
    )
    child = ModelDeclaration(
        "Child",
        (EX + "Child",),
        (
            FieldDeclaration(
                "parent", EX + "parent", (ValueDeclaration("object", model="Parent"),)
            ),
            FieldDeclaration(
                "length",
                EX + "length",
                (ValueDeclaration("literal", datatype=str(XSD.decimal)),),
            ),
        ),
    )
    result = index_uses(
        [TermDeclaration(EX + "registered", location="registry")], [parent, child]
    )
    assert result.report.verified
    assert {(use.model, use.field) for use in result.uses(EX + "Child")} == {
        ("Child", None),
        ("Parent", "child"),
    }
    referring = next(use for use in result.uses(EX + "Child") if use.field)
    assert referring.reverse and referring.branch == 0
    assert result.uses(str(XSD.decimal))[0].location == "Child.length"
    assert result.uses(EX + "registered")[0].location == "registry"


def test_context_references_come_from_actual_expansion_not_instance_identifiers() -> (
    None
):
    document = {
        "@context": {
            "@vocab": EX,
            "parent": {"@reverse": EX + "parent", "@type": "@id"},
            "value": {"@id": EX + "value", "@type": str(XSD.decimal)},
        },
        "@id": EX + "instance",
        "@type": "Record",
        "parent": EX + "other-instance",
        "value": "1.00",
    }
    prepared = prepare_usages(document)
    result = index_uses(
        observed_usages=[
            prepared.observe(("@type",)),
            prepared.observe(("parent",)),
            prepared.observe(("value",)),
        ]
    )
    assert result.report.verified
    assert result.uses(EX + "Record")[0].role == "class"
    assert result.uses(EX + "parent")[0].reverse
    assert result.uses(str(XSD.decimal))[0].path == ("value",)
    assert not result.uses(EX + "instance")
    assert not result.uses(EX + "other-instance")


def test_unavailable_usage_and_missing_children_remain_explicit() -> None:
    model = ModelDeclaration(
        "Parent",
        (),
        (
            FieldDeclaration(
                "child", EX + "child", (ValueDeclaration("object", model="Missing"),)
            ),
        ),
    )
    result = index_uses(models=[model], observed_usages=[ObservedUsage(("missing",))])
    assert result.report.outcome == "unverified"
    assert {issue.code for issue in result.report.issues} == {
        "upgrade.child_model_unavailable",
        "upgrade.usage_unavailable",
    }
    assert result.uses(EX + "child")
    limited = index_uses(
        [TermDeclaration(EX + "A"), TermDeclaration(EX + "B")],
        limits=Limits(max_work=2),
    )
    assert limited.report.outcome == "exhausted"
    assert [use.iri for use in limited.entries] == [EX + "A"]


def test_observed_uses_retain_anonymous_versus_default_graph_scope() -> None:
    for keyword, anonymous in (("@graph", True), (EX + "nodes", False)):
        document = {
            "@context": {"@vocab": EX, "nodes": keyword},
            "items": {"nodes": {"@id": EX + "record", "p": "value"}},
        }
        usage = prepare_usages(document).observe(("items", "nodes", "p"))
        result = index_uses(observed_usages=[usage])
        assert result.report.verified
        assert result.entries
        assert all(use.graph is None for use in result.entries)
        assert all(use.anonymous_graph is anonymous for use in result.entries)
        assert all(
            (use.graph_identity is not None) is anonymous for use in result.entries
        )
