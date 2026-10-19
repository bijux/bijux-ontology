"""Typed regular paths retain real axioms and cannot manufacture existential edges."""

from dataclasses import replace
from typing import Any, cast

from rdflib import BNode, Dataset, Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.browsing.results import Selection
from bijux_ontology.vocabulary.relation_paths import (
    RelationChoice,
    RelationEndpoint,
    RelationInverse,
    RelationRepeat,
    RelationSequence,
    RelationStep,
    check_relation_path,
    find_relation_path,
    relation_enumeration,
)
from bijux_ontology.vocabulary.restrictions import project_restrictions
from bijux_ontology.vocabulary.restrictions.types import RestrictionProjection
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/paths/")
ISA = RelationStep(str(RDFS.subClassOf), "class")
PART = RelationStep(str(EX.part_of), "existential")
LANGUAGE = RelationSequence((RelationRepeat(ISA), PART, RelationRepeat(ISA)))


def _restriction(
    graph: Graph,
    owner: URIRef,
    target: URIRef,
    quantifier: URIRef = OWL.someValuesFrom,
    predicate: URIRef = EX.part_of,
) -> None:
    node = BNode()
    for triple in (
        (owner, RDFS.subClassOf, node),
        (node, RDF.type, OWL.Restriction),
        (node, OWL.onProperty, predicate),
        (node, quantifier, target),
    ):
        graph.add(triple)


def _graph() -> Graph:
    graph = Graph()
    graph.add((EX.Small, RDFS.subClassOf, EX.Component))
    graph.add((EX.Cell, RDFS.subClassOf, EX.Organism))
    _restriction(graph, EX.Component, EX.Cell)
    _restriction(graph, EX.Universal, EX.Cell, OWL.allValuesFrom)
    graph.add((EX.Raw, EX.part_of, EX.Cell))
    _restriction(graph, EX.Other, EX.Cell, predicate=EX.located_in)
    return graph


def test_typed_language_requires_real_existential_and_retains_authored_edges() -> None:
    view = inspect_vocabulary(_graph())
    goal = RelationEndpoint((str(EX.Organism),))
    result = check_relation_path(view, str(EX.Small), LANGUAGE, endpoint=goal)
    assert result.status == "found" and result.report.verified
    assert len(result.steps) == 3
    assert [step.relation.role for step in result.steps] == [
        "class",
        "existential",
        "class",
    ]
    projection = result.steps[1].evidence
    assert isinstance(projection, RestrictionProjection)
    assert projection.quantifier == OWL.someValuesFrom
    assert projection.authored_owner == EX.Component
    assert (EX.Component, EX.part_of, EX.Cell) not in view.graph()
    for source in (EX.Universal, EX.Raw, EX.Other):
        failed = check_relation_path(view, str(source), LANGUAGE, endpoint=goal)
        assert failed.status == "nonmatch" and failed.report.outcome == "disagreement"
        observed = find_relation_path(view, str(source), LANGUAGE, endpoint=goal)
        assert observed.status == "nonmatch" and observed.report.verified
        assert not observed.steps and observed.endpoint is None
    raw = check_relation_path(
        view,
        str(EX.Raw),
        RelationStep(str(EX.part_of), "asserted"),
        endpoint=RelationEndpoint((str(EX.Cell),)),
    )
    assert raw.report.verified and raw.steps[0].relation.role == "asserted"
    wrong = check_relation_path(
        view, str(EX.Raw), RelationStep(str(EX.part_of), "class"), endpoint=goal
    )
    assert wrong.report.outcome == "input_error"


def test_sequence_choice_inverse_repetition_cycles_and_missing_zero_length() -> None:
    graph = _graph()
    graph.add((EX.Component, RDFS.subClassOf, EX.Small))
    graph.add((EX.Child, SKOS.broader, EX.Parent))
    graph.add((EX.Parent, RDF.type, SKOS.Concept))
    view = inspect_vocabulary(graph)
    inverse = check_relation_path(
        view,
        str(EX.Organism),
        RelationInverse(LANGUAGE),
        endpoint=RelationEndpoint((str(EX.Small),)),
    )
    assert inverse.report.verified and all(step.inverse for step in inverse.steps)
    selected = RelationChoice((RelationStep(str(SKOS.broader), "skos"), ISA))
    assert check_relation_path(
        view,
        str(EX.Child),
        selected,
        endpoint=RelationEndpoint(declared_kinds=(str(SKOS.Concept),)),
    ).report.verified
    zero = check_relation_path(
        view,
        str(EX.Small),
        RelationRepeat(ISA, 0, 0),
        endpoint=RelationEndpoint((str(EX.Small),)),
    )
    assert zero.report.verified and not zero.steps
    missing = check_relation_path(
        view,
        str(EX.Missing),
        RelationRepeat(ISA),
        endpoint=RelationEndpoint((str(EX.Missing),)),
    )
    assert missing.status == "incomplete" and not missing.report.verified
    dead = check_relation_path(
        view,
        str(EX.Small),
        RelationRepeat(ISA),
        endpoint=RelationEndpoint((str(EX.Other),)),
    )
    assert dead.status == "nonmatch"
    bound = check_relation_path(
        view,
        str(EX.Small),
        LANGUAGE,
        endpoint=RelationEndpoint((str(EX.Organism),)),
        max_depth=1,
    )
    assert bound.status == "incomplete" and any(
        i.kind == "exhausted" for i in bound.report.issues
    )
    one = check_relation_path(
        view,
        str(EX.Small),
        RelationRepeat(ISA, 2, 2),
        endpoint=RelationEndpoint((str(EX.Small),)),
    )
    assert one.report.verified and len(one.steps) == 2
    bad = check_relation_path(
        view,
        str(EX.Small),
        RelationRepeat(ISA, True),
        endpoint=RelationEndpoint((str(EX.Small),)),
    )
    assert bad.report.outcome == "input_error"


def test_caller_projection_scope_quantifiers_and_blank_ownership_remain_required() -> (
    None
):
    view = inspect_vocabulary(_graph())
    actual = project_restrictions(view, str(EX.Component))
    endpoint = RelationEndpoint((str(EX.Cell),))
    assert check_relation_path(
        view, str(EX.Component), PART, endpoint=endpoint, projections=actual
    ).report.verified
    unknown = replace(
        actual,
        report=Report(
            "supplied",
            1,
            (
                Issue(
                    "example.unknown",
                    "Incomplete projection capability",
                    kind="unverified",
                ),
            ),
            complete=False,
            requested=2,
        ),
    )
    result = check_relation_path(
        view, str(EX.Component), PART, endpoint=endpoint, projections=unknown
    )
    assert result.status == "incomplete" and any(
        i.code == "example.unknown" for i in result.report.issues
    )
    all_values = project_restrictions(view, str(EX.Universal))
    assert (
        check_relation_path(
            view, str(EX.Universal), PART, endpoint=endpoint, projections=all_values
        ).status
        == "nonmatch"
    )
    forged = replace(all_values.items[0], quantifier=OWL.someValuesFrom)
    assert not check_relation_path(
        view,
        str(EX.Universal),
        PART,
        endpoint=endpoint,
        projections=Selection((forged,), Report("supplied", 1)),
    ).report.verified
    oversized = Selection(actual.items, Report("x" * 100000, 1))
    bounded = check_relation_path(
        view,
        str(EX.Component),
        PART,
        endpoint=endpoint,
        projections=oversized,
        budget=Budget(Limits(max_bytes=2000)),
    )
    assert bounded.status == "incomplete"
    assert bounded.report.outcome == "exhausted"
    assert all(len(child.scope) < 2000 for child in bounded.report.children)
    oversized_enum = ValueEnumeration((EX.Cell,), report=Report("x" * 100000, 1))
    bounded = check_relation_path(
        view,
        str(EX.Component),
        PART,
        endpoint=RelationEndpoint(enumeration=oversized_enum),
        budget=Budget(Limits(max_bytes=2000)),
    )
    assert bounded.report.outcome == "exhausted"
    assert all(len(child.scope) < 2000 for child in bounded.report.children)
    record = actual.items[0]
    assert isinstance(record.expression, BNode)
    detached = BNode(str(record.expression))
    assert detached is not record.expression
    foreign = replace(record, expression=detached)
    assert not check_relation_path(
        view,
        str(EX.Component),
        PART,
        endpoint=endpoint,
        projections=Selection((foreign,), Report("supplied", 1)),
    ).report.verified


def test_selected_physical_and_import_scopes_and_exact_iris() -> None:
    dataset = Dataset()
    selected = dataset.graph(EX.G)
    hidden = dataset.graph(EX.H)
    selected.add((EX.Small, RDFS.subClassOf, EX.Component))
    hidden.add((EX.Component, RDFS.subClassOf, EX.Cell))
    pinned = inspect_vocabulary(load_vocabulary(dataset, graph_name=str(EX.G)))
    failed = check_relation_path(
        pinned,
        str(EX.Small),
        RelationRepeat(ISA),
        endpoint=RelationEndpoint((str(EX.Cell),)),
    )
    assert failed.status == "nonmatch"
    union = inspect_vocabulary(load_vocabulary(dataset, union=True))
    assert check_relation_path(
        union,
        str(EX.Small),
        RelationRepeat(ISA),
        endpoint=RelationEndpoint((str(EX.Cell),)),
    ).report.verified
    graph = Graph().add(
        (EX.Small, RDFS.subClassOf, URIRef("http://example.org/paths/Cell"))
    )
    assert (
        check_relation_path(
            inspect_vocabulary(graph),
            str(EX.Small),
            ISA,
            endpoint=RelationEndpoint((str(EX.Cell),)),
        ).status
        == "nonmatch"
    )


def test_finite_enumeration_native_application_and_operation_limits() -> None:
    from bijux_ontology.declarations.types import (
        FieldDeclaration,
        ModelDeclaration,
        ValueDeclaration,
    )
    from bijux_ontology.declarations.value_constraints import ValueConstraints
    from bijux_ontology.documents.preparation import prepare_source
    from bijux_ontology.models.emission.rdf import check_rdf_emission
    from bijux_ontology.models.emission.types import RdfModelUsage

    graph = _graph()
    for triple in (
        (EX.Record, RDF.type, OWL.Class),
        (EX.selected, RDF.type, OWL.ObjectProperty),
    ):
        graph.add(triple)
    view = inspect_vocabulary(graph)
    members = relation_enumeration(
        view,
        [str(EX.Small), str(EX.Universal)],
        LANGUAGE,
        endpoint=RelationEndpoint((str(EX.Organism),)),
    )
    assert (
        members.qualified
        and members.values == (EX.Small,)
        and members.excluded == (EX.Universal,)
    )
    field = FieldDeclaration(
        "selected",
        str(EX.selected),
        (ValueDeclaration("iri", constraints=ValueConstraints(enumeration=members)),),
    )
    model = ModelDeclaration("Record", (str(EX.Record),), (field,))
    for value, valid in ((EX.Small, True), (EX.Universal, False)):
        data = (
            Graph()
            .add((EX.record, RDF.type, EX.Record))
            .add((EX.record, EX.selected, value))
        )
        actual = check_rdf_emission(
            prepare_source(data), (model,), (RdfModelUsage("Record", EX.record),)
        )
        assert actual.verified is valid
    opened = RelationEndpoint(
        enumeration=ValueEnumeration((EX.Organism,), closed=False)
    )
    assert (
        check_relation_path(view, str(EX.Small), LANGUAGE, endpoint=opened).status
        == "incomplete"
    )
    for cap in (1, 10, 50, 200, 1000):
        result = check_relation_path(
            view,
            str(EX.Small),
            LANGUAGE,
            endpoint=RelationEndpoint((str(EX.Organism),)),
            budget=Budget(Limits(max_work=cap)),
        )
        assert result.status == "incomplete" and any(
            i.kind == "exhausted" for i in result.report.issues
        )
    enormous = check_relation_path(
        view,
        str(EX.Small),
        RelationRepeat(ISA, 10**1000),
        endpoint=RelationEndpoint((str(EX.Small),)),
    )
    assert enormous.status == "incomplete" and any(
        i.kind == "exhausted" for i in enormous.report.issues
    )
    spent = Budget(Limits(max_issues=1))
    spent.consume("issues")
    result = check_relation_path(
        view,
        cast(Any, BNode("urn:bad")),
        ISA,
        endpoint=RelationEndpoint((str(EX.Small),)),
        budget=spent,
    )
    assert result.report.outcome == "exhausted"
    assert check_relation_path(
        view, str(EX.Small), LANGUAGE, endpoint=RelationEndpoint((str(EX.Organism),))
    ).report.verified
