"""Native restriction records preserve quantifiers, authored scope and witnesses."""

from typing import Any

import pytest
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.restrictions import (
    existential_path,
    project_restrictions,
)
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/restrictions/")


def restriction(
    graph: Graph, owner: URIRef, quantifier: URIRef, filler: Any, *, node: Any = None
) -> Any:
    """Attach one actual authored restriction, without a direct relation triple."""
    node = node if node is not None else BNode()
    graph.add((owner, RDFS.subClassOf, node))
    graph.add((node, RDF.type, OWL.Restriction))
    graph.add((node, OWL.onProperty, EX.part_of))
    graph.add((node, quantifier, filler))
    return node


def test_existential_inheritance_keeps_actual_owners_and_never_invents_assertions() -> (
    None
):
    graph = Graph().add((EX.Component, RDFS.subClassOf, EX.Parent))
    restriction(graph, EX.Parent, OWL.someValuesFrom, EX.Cell)
    restriction(graph, EX.Cell, OWL.someValuesFrom, EX.Organism)
    view = inspect_vocabulary(graph)
    assert not project_restrictions(view, str(EX.Component)).items
    projected = project_restrictions(view, str(EX.Component), inherited=True)
    assert projected.report.verified and len(projected.items) == 1
    item = projected.items[0]
    assert item.owner == EX.Component and item.authored_owner == EX.Parent
    assert item.axiom.subject == EX.Parent and item.axiom.predicate == RDFS.subClassOf
    assert item.inheritance[0].subject == EX.Component
    assert item.inheritance[0].object == EX.Parent
    assert item.property == EX.part_of and item.quantifier == OWL.someValuesFrom
    assert item.filler == EX.Cell and item.status == "supported"
    path = existential_path(
        view, str(EX.Component), str(EX.Organism), properties=[str(EX.part_of)]
    )
    assert path.status == "found" and path.report.verified and len(path.steps) == 2
    assert path.steps[0].inheritance == item.inheritance
    assert (
        existential_path(
            view, str(EX.Component), str(EX.Cell), properties=[str(EX.other)]
        ).status
        == "unreachable"
    )
    assert (EX.Component, EX.part_of, EX.Cell) not in view.graph()
    mixed = view.graph()
    mixed.add((EX.MixedChild, RDFS.subClassOf, EX.NamedExpression))
    mixed.add((EX.NamedExpression, OWL.onProperty, EX.part_of))
    mixed.add((EX.NamedExpression, OWL.allValuesFrom, EX.Other))
    mixed.add((EX.NamedExpression, RDFS.subClassOf, EX.Parent))
    inherited = existential_path(
        inspect_vocabulary(mixed),
        str(EX.MixedChild),
        str(EX.Cell),
        properties=[str(EX.part_of)],
    )
    assert inherited.status == "found" and inherited.report.verified
    step = inherited.steps[0]
    assert step.owner == EX.MixedChild and step.authored_owner == EX.Parent
    assert [edge.subject for edge in step.inheritance] == [
        EX.MixedChild,
        EX.NamedExpression,
    ]
    head = BNode()
    intersection = BNode()
    direct = restriction(mixed, EX.Unrelated, OWL.someValuesFrom, EX.Other)
    Collection(mixed, head, [EX.Parent, direct])
    mixed.add((EX.ConjunctChild, RDFS.subClassOf, intersection))
    mixed.add((intersection, OWL.intersectionOf, head))
    conjunct_view = inspect_vocabulary(mixed)
    conjunct = existential_path(
        conjunct_view,
        str(EX.ConjunctChild),
        str(EX.Cell),
        properties=[str(EX.part_of)],
        inherited=True,
    )
    assert conjunct.status == "found" and conjunct.report.verified
    step = conjunct.steps[0]
    assert step.owner == EX.ConjunctChild and step.authored_owner == EX.Parent
    assert step.axiom.subject == EX.Parent
    assert any(edge.predicate == OWL.intersectionOf for edge in step.inheritance)
    assert any(
        edge.predicate == RDF.first and edge.object == EX.Parent
        for edge in step.inheritance
    )
    supplied = conjunct_view.graph()
    assert all(
        (edge.subject, edge.predicate, edge.object) in supplied
        for edge in step.inheritance
    )
    assert (EX.ConjunctChild, RDFS.subClassOf, EX.Parent) not in supplied
    assert (
        existential_path(
            conjunct_view,
            str(EX.ConjunctChild),
            str(EX.Cell),
            properties=[str(EX.part_of)],
            inherited=False,
        ).status
        == "unreachable"
    )
    graph.remove((None, None, None))
    assert (
        project_restrictions(view, str(EX.Component), inherited=True).items
        == projected.items
    )


def test_intersections_preserve_quantifiers_and_do_not_flatten_union_fillers() -> None:
    graph = Graph()
    some = restriction(graph, EX.Unrelated, OWL.someValuesFrom, EX.Cell)
    universal = restriction(graph, EX.Unrelated, OWL.allValuesFrom, EX.Tissue)
    value = restriction(graph, EX.Unrelated, OWL.hasValue, Literal("01"))
    head = BNode()
    Collection(graph, head, [some, universal, value, EX.Named])
    graph.add((EX.Named, RDFS.subClassOf, some))
    intersection = BNode()
    graph.add((EX.Component, OWL.equivalentClass, intersection))
    graph.add((intersection, OWL.intersectionOf, head))
    view = inspect_vocabulary(graph)
    result = project_restrictions(view, str(EX.Component))
    assert result.report.verified
    assert {r.quantifier for r in result.items} == {
        OWL.someValuesFrom,
        OWL.allValuesFrom,
        OWL.hasValue,
    }
    assert all(any(e.predicate == RDF.first for e in r.structure) for r in result.items)
    for work in (100, 150, 200, 250, 300):
        bounded = project_restrictions(
            view, str(EX.Component), budget=Budget(Limits(max_work=work))
        )
        assert bounded.report.outcome in {"exhausted", "agreement"}
        assert not any(
            issue.code == "terms.restriction_malformed"
            for issue in bounded.report.issues
        )
    assert (
        existential_path(
            view, str(EX.Component), str(EX.Cell), properties=[str(EX.part_of)]
        ).status
        == "found"
    )
    assert (
        existential_path(
            view, str(EX.Component), str(EX.Tissue), properties=[str(EX.part_of)]
        ).status
        == "unreachable"
    )
    union = BNode()
    union_head = BNode()
    Collection(graph, union_head, [EX.Cell, EX.Tissue])
    graph.add((union, OWL.unionOf, union_head))
    restriction(graph, EX.Component, OWL.someValuesFrom, union)
    uncertain = existential_path(
        inspect_vocabulary(graph),
        str(EX.Component),
        str(EX.Absent),
        properties=[str(EX.part_of)],
    )
    assert uncertain.status == "incomplete" and not uncertain.report.verified
    found = existential_path(
        inspect_vocabulary(graph),
        str(EX.Component),
        str(EX.Cell),
        properties=[str(EX.part_of)],
    )
    assert found.status == "found" and not found.report.complete
    assert any(i.kind == "unverified" for i in found.report.issues)


def test_named_expression_components_remain_in_their_authored_source() -> None:
    local = Graph().add((EX.Component, RDFS.subClassOf, EX.Expression))
    local.add((EX.Expression, RDF.type, OWL.Restriction))
    local.add((EX.Expression, OWL.onProperty, EX.part_of))
    imported = Graph().add((EX.Expression, OWL.someValuesFrom, EX.Cell))
    source = Vocabulary(local, imports=(imported,))
    view = inspect_vocabulary(source)
    result = project_restrictions(view, str(EX.Component))
    assert len(result.items) == 1 and result.items[0].status == "malformed"
    assert all(edge.scope == "local" for edge in result.items[0].structure)
    assert (
        existential_path(
            view, str(EX.Component), str(EX.Cell), properties=[str(EX.part_of)]
        ).status
        == "incomplete"
    )
    imported.add((EX.Expression, OWL.onProperty, EX.part_of))
    imported.add((EX.Component, RDFS.subClassOf, EX.Expression))
    own_import = inspect_vocabulary(Vocabulary(local, imports=(imported,)))
    path = existential_path(
        own_import, str(EX.Component), str(EX.Cell), properties=[str(EX.part_of)]
    )
    assert path.status == "found" and not path.report.verified
    assert path.steps[0].axiom.scope == "import[0]"
    assert (
        existential_path(
            inspect_vocabulary(Vocabulary(local, lookup=(imported,))),
            str(EX.Component),
            str(EX.Cell),
            properties=[str(EX.part_of)],
        ).status
        == "incomplete"
    )


def test_cycles_and_bounds_retain_findings_without_proving_absence() -> None:
    graph = Graph()
    restriction(graph, EX.Component, OWL.someValuesFrom, EX.Cell, node=EX.AValid)
    malformed = restriction(
        graph, EX.Component, OWL.someValuesFrom, EX.Absent, node=EX.BMalformed
    )
    graph.add((malformed, OWL.allValuesFrom, EX.Other))
    for index in range(20):
        restriction(
            graph,
            EX.Component,
            OWL.someValuesFrom,
            EX.Cell,
            node=URIRef(str(EX) + f"Z{index}"),
        )
    view = inspect_vocabulary(graph)
    observed = False
    for work in range(250, 1100, 25):
        result = project_restrictions(
            view, str(EX.Component), budget=Budget(Limits(max_work=work))
        )
        if result.report.outcome == "exhausted" and any(
            r.status == "malformed" for r in result.items
        ):
            assert result.items[0].status == "supported"
            assert any(
                i.code == "terms.restriction_malformed" for i in result.report.issues
            )
            observed = True
            break
    assert observed
    cyclic = Graph()
    expression = BNode()
    head = BNode()
    Collection(cyclic, head, [EX.Named, expression])
    cyclic.add((EX.Component, RDFS.subClassOf, expression))
    cyclic.add((expression, OWL.intersectionOf, head))
    cycle = existential_path(
        inspect_vocabulary(cyclic),
        str(EX.Component),
        str(EX.Cell),
        properties=[str(EX.part_of)],
    )
    assert cycle.status == "incomplete"
    assert any(i.code == "terms.restriction_malformed" for i in cycle.report.issues)
    shallow = existential_path(
        view, str(EX.Component), str(EX.Cell), properties=[str(EX.part_of)], max_depth=0
    )
    assert shallow.status == "incomplete" and shallow.report.outcome == "exhausted"
    broken = Graph()
    broken.add((EX.Component, RDFS.subClassOf, EX.Expression))
    broken.add((EX.Expression, OWL.intersectionOf, head))
    broken.add((head, RDF.first, EX.Named))
    assert (
        project_restrictions(inspect_vocabulary(broken), str(EX.Component))
        .items[0]
        .status
        == "malformed"
    )


def test_runtime_boundaries_and_supported_named_cycles_remain_finite() -> None:
    graph = Graph().add((EX.Component, RDFS.subClassOf, EX.Parent))
    graph.add((EX.Parent, RDFS.subClassOf, EX.Component))
    restriction(graph, EX.Parent, OWL.someValuesFrom, EX.Cell)
    view = inspect_vocabulary(graph)
    assert len(project_restrictions(view, str(EX.Component), inherited=True).items) == 1
    for owner in (BNode(), Literal(str(EX.Component)), 1):
        with pytest.raises(InputError):
            project_restrictions(view, owner)  # type: ignore[arg-type]
    invalid: tuple[Any, ...] = (
        None,
        str(EX.part_of),
        {},
        iter([str(EX.part_of)]),
        [1],
        [],
    )
    for properties in invalid:
        with pytest.raises(InputError):
            existential_path(
                view, str(EX.Component), str(EX.Cell), properties=properties
            )
    with pytest.raises(InputError):
        project_restrictions(view, str(EX.Component), inherited=1)  # type: ignore[arg-type]
