"""Finite contextual propagation and explicit co-emission promises."""

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations import FieldDeclaration as Field
from bijux_ontology.declarations import ModelDeclaration as Model
from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.models.contracts.checking import check_models

EX = Namespace("https://example.org/")


def graph_contracts() -> Graph:
    graph = Graph()
    for cls in (EX.Root, EX.Child, EX.Left, EX.Right):
        graph.add((cls, RDF.type, OWL.Class))
    graph.add((EX.Left, OWL.disjointWith, EX.Right))
    for prop in (EX.left, EX.right, EX.good, EX.bad, EX.loop):
        graph.add((prop, RDF.type, RDF.Property))
    graph.add((EX.left, RDFS.domain, EX.Left))
    graph.add((EX.right, RDFS.domain, EX.Right))
    graph.add((EX.good, RDFS.range, EX.Left))
    graph.add((EX.bad, RDFS.range, EX.Right))
    return graph


@pytest.mark.parametrize("guaranteed", [True, False])
def test_only_explicit_emission_promises_prove_simultaneous_conflicts(
    guaranteed: bool,
) -> None:
    fields = tuple(
        Field(
            name,
            str(EX[name]),
            (Value("iri"),),
            emission="guaranteed" if guaranteed else "optional",
        )
        for name in ("left", "right")
    )
    report = check_models(
        (Model("child", (str(EX.Child),), fields),), graph_contracts()
    )
    if guaranteed:
        assert any(
            issue.code == "models.guaranteed_emission_conflict"
            for issue in report.issues
        )
    else:
        assert not any(
            issue.code == "models.guaranteed_emission_conflict"
            for issue in report.issues
        )
        assert any(
            issue.code == "models.conditional_emission_unverified"
            and "left, right" in issue.message
            for issue in report.issues
        )


def test_reused_recursive_child_keeps_incoming_constraints_separate() -> None:
    child = Model(
        "child",
        (str(EX.Child),),
        (
            Field("left", str(EX.left), (Value("iri"),), emission="guaranteed"),
            Field("loop", str(EX.loop), (Value("object", model="child"),)),
        ),
    )
    root = Model(
        "root",
        (str(EX.Root),),
        (
            Field("good", str(EX.good), (Value("object", model="child"),)),
            Field("bad", str(EX.bad), (Value("object", model="child"),)),
        ),
    )
    report = check_models((root, child), graph_contracts())
    conflicts = [
        issue
        for issue in report.issues
        if issue.code == "models.incoming_contract_conflict"
    ]
    assert conflicts and all(
        issue.location.startswith("root.bad[0]") for issue in conflicts
    )
    assert not any(
        issue.location.startswith("root.good[0]") and issue.kind == "finding"
        for issue in report.issues
    )
    assert report.checked < 100


def test_alternative_branch_types_are_not_combined_as_simultaneous() -> None:
    graph = graph_contracts()
    field = Field(
        "choice",
        str(EX.loop),
        (
            Value("object", types=(str(EX.Left),)),
            Value("object", types=(str(EX.Right),)),
        ),
        emission="guaranteed",
    )
    assert check_models((Model("root", (str(EX.Root),), (field,)),), graph).verified


def test_work_exhaustion_preserves_completed_input_findings() -> None:
    from bijux_ontology.execution.limits import Limits

    root = Model(
        "root",
        (str(EX.Root),),
        (Field("child", str(EX.good), (Value("object", model="missing"),)),),
    )
    report = check_models((root,), graph_contracts(), limits=Limits(max_work=100))
    assert report.complete is False
    assert report.exit_code() == 2
    assert any(issue.code == "input.model_reference_missing" for issue in report.issues)
    assert any(issue.kind == "exhausted" for issue in report.issues)


def test_recursive_states_and_diagnostic_retention_have_explicit_bounds() -> None:
    from bijux_ontology.execution.limits import Limits

    child = Model(
        "child",
        (str(EX.Child),),
        (Field("loop", str(EX.loop), (Value("object", model="child"),)),),
    )
    root = Model(
        "root",
        (str(EX.Root),),
        (Field("child", str(EX.good), (Value("object", model="child"),)),),
    )
    report = check_models((root, child), graph_contracts(), limits=Limits(max_states=1))
    assert not report.complete and report.exit_code() == 2
    unknown = Model(
        "unknown",
        (str(EX.Missing),),
        (Field("missing", str(EX.missing), (Value("unknown"),)),),
    )
    report = check_models((unknown,), graph_contracts(), limits=Limits(max_issues=1))
    assert report.issues[0].code == "models.value_unverified"
    assert len(report.issues) == 2
    assert report.issues[-1].kind == "exhausted"


def test_list_ranges_constrain_heads_while_item_types_remain_independent() -> None:
    graph = graph_contracts()
    graph.add((EX.good, RDFS.range, RDF.List))
    graph.remove((EX.good, RDFS.range, EX.Left))
    graph.add((EX.Right, OWL.disjointWith, RDF.List))
    field = Field(
        "items",
        str(EX.good),
        (Value("object", types=(str(EX.Right),), container="@list"),),
    )
    report = check_models((Model("root", (str(EX.Root),), (field,)),), graph)
    assert report.verified
    direct = Field(
        "items",
        str(EX.good),
        (Value("object", types=(str(EX.Right),), container="@set"),),
    )
    report = check_models((Model("root", (str(EX.Root),), (direct,)),), graph)
    assert any(issue.code == "models.disjoint_conflict" for issue in report.issues)
    graph.add((EX.good, RDF.type, OWL.DatatypeProperty))
    report = check_models((Model("root", (str(EX.Root),), (field,)),), graph)
    assert any(
        issue.code == "models.list_head_kind_conflict"
        and issue.location.endswith(".head")
        for issue in report.issues
    )


def test_disjoint_group_selection_includes_implicit_rdf_list_heads() -> None:
    from rdflib import BNode
    from rdflib.collection import Collection

    graph = graph_contracts()
    group, head = BNode(), BNode()
    graph.add((group, RDF.type, OWL.AllDisjointClasses))
    graph.add((group, OWL.members, head))
    Collection(graph, head, [RDF.List, EX.Left])
    field = Field(
        "items",
        str(EX.good),
        (Value("object", types=(str(EX.Right),), container="@list"),),
    )
    report = check_models((Model("root", (str(EX.Root),), (field,)),), graph)
    assert any(
        issue.code == "models.disjoint_conflict"
        and issue.location == "root.items[0].head"
        for issue in report.issues
    )
