"""Explicit classification policies over real RDF, separate from world claims."""

from dataclasses import replace
from threading import Event

from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import RDF, SKOS, XSD

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.xkos.classifications import (
    ClassificationPolicy,
    LevelCodePolicy,
    PartitionContract,
    check_classification,
    read_classification,
)
from bijux_ontology.interfaces.adapters.xkos.classifications.types import XKOS
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.skos.checking import check_skos

N = Namespace("urn:classification:")


def _graph() -> Graph:
    """Two authored levels, distinct relation meanings and an assigned unit."""
    return Graph().parse(
        data="""
        @prefix n: <urn:classification:> .
        @prefix x: <http://rdf-vocabulary.ddialliance.org/xkos#> .
        @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
        n:release a skos:ConceptScheme; x:levels (n:aggregate n:detail);
          x:numberOfLevels 2; x:coversExhaustively n:domain;
          x:coversMutuallyExclusively n:domain .
        n:aggregate a x:ClassificationLevel; x:depth 1; skos:member n:A .
        n:detail a x:ClassificationLevel; x:depth 2; skos:member n:B, n:C .
        n:A a skos:Concept; skos:notation "A"; skos:prefLabel "Aggregate"@en .
        n:B a skos:Concept; skos:notation "01"; x:specializes n:A .
        n:C a skos:Concept; skos:notation "02"; x:isPartOf n:A .
        n:unit x:classifiedUnder n:B .
    """,
        format="turtle",
    )


def _codes(report: Report) -> set[str]:
    """Use retained diagnostics rather than interpreting their prose."""
    return {issue.code for issue in report.issues}


def test_selected_levels_codes_and_original_terms() -> None:
    """A finite real classification supplies a usable exact code-list projection."""
    graph = _graph()
    before = set(graph)
    view = read_classification(graph, N.release)
    checked = check_classification(
        view,
        ClassificationPolicy(
            tree_relations=("generic", "partitive"),
            codes=(
                LevelCodePolicy(
                    N.detail, allowed=ValueEnumeration((Literal("01"), Literal("02")))
                ),
            ),
        ),
    )
    assert view.report.verified and checked.report.verified, checked.report.to_dict()
    assert view.ordered_levels == (N.aggregate, N.detail)
    assert {(row.category, row.position) for row in checked.rows} == {
        (N.A, 1),
        (N.B, 2),
        (N.C, 2),
    }
    assert next(row for row in checked.rows if row.category == N.B).codes == (
        Literal("01"),
    )
    assert {link.kind for link in view.hierarchy} == {"generic", "partitive"}
    assert len(view.coverage) == 2 and checked.partition is None
    assert set(graph) == before


def test_depth_membership_and_selected_tree_findings() -> None:
    """Wrong depth, conflicting levels and actual cycles are independent findings."""
    graph = _graph()
    graph.set((N.detail, XKOS.depth, Literal(3)))
    graph.add((N.aggregate, SKOS.member, N.B))
    graph.add((N.A, XKOS.specializes, N.B))
    report = check_classification(
        read_classification(graph, N.release),
        ClassificationPolicy(tree_relations=("generic",)),
    ).report
    assert not report.verified
    assert {
        "xkos.depth_mismatch",
        "xkos.category_level_conflict",
        "xkos.tree_cycle",
    } <= _codes(report)
    ordinary = Graph()
    ordinary.add((N.B, SKOS.broader, N.A))
    ordinary.add((N.B, SKOS.broader, N.C))
    assert check_skos(ordinary).verified
    assert check_classification(
        read_classification(_graph(), N.release)
    ).report.verified


def test_selection_excludes_unrelated_duties_keeps_ancestors() -> None:
    """Required rows get code duties; supporting parents do not acquire them."""
    graph = _graph()
    graph.remove((N.A, SKOS.notation, None))
    graph.remove((N.C, SKOS.notation, None))
    graph.add((N.C, XKOS.specializes, N.C))
    view = read_classification(graph, N.release, categories=(N.B,))
    result = check_classification(
        view,
        ClassificationPolicy(
            required_levels=(N.detail,),
            tree_relations=("generic",),
            codes=(LevelCodePolicy(N.detail),),
        ),
    )
    assert result.report.verified, result.report.to_dict()
    assert tuple(row.category for row in result.rows) == (N.B,)
    unknown = check_classification(
        read_classification(graph, N.release, categories=(N.absent,))
    )
    assert not unknown.report.verified
    assert "xkos.category_level_unverified" in _codes(unknown.report)


def test_finite_partition_requires_closed_assignments_selected_relation() -> None:
    """Coverage declarations cannot replace explicit finite assignment evidence."""
    graph = _graph()
    view = read_classification(graph, N.release)
    contract = PartitionContract(
        (N.aggregate, N.detail), (N.unit,), True, propagation=("generic",)
    )
    good = check_classification(view, ClassificationPolicy(partition=contract))
    assert (
        good.report.verified and good.partition is not None and good.partition.verified
    )
    partitive = check_classification(
        view,
        ClassificationPolicy(partition=replace(contract, propagation=("partitive",))),
    )
    assert "xkos.population_unassigned" in _codes(partitive.report)
    for incomplete in (
        replace(contract, population=None),
        replace(contract, assignments_complete=False),
    ):
        result = check_classification(view, ClassificationPolicy(partition=incomplete))
        assert not result.report.verified
        assert result.partition is not None and not result.partition.complete
    graph.add((N.unit, XKOS.classifiedUnder, N.C))
    overlap = check_classification(
        read_classification(graph, N.release),
        ClassificationPolicy(partition=replace(contract, assignments_complete=False)),
    )
    assert {"xkos.population_overlap", "xkos.assignments_open"} <= _codes(
        overlap.report
    )
    graph = _graph()
    graph.set((N.unit, XKOS.classifiedUnder, N.external))
    unresolved = check_classification(
        read_classification(graph, N.release), ClassificationPolicy(partition=contract)
    )
    assert "xkos.assignment_unverified" in _codes(unresolved.report)
    assert "xkos.population_unassigned" not in _codes(unresolved.report)


def test_physical_scope_blank_identity_and_declared_dialect() -> None:
    """No union shortcut, blank label join or guessed notation-pattern dialect."""
    dataset = Dataset()
    for triple in _graph():
        dataset.graph(N.physical).add(triple)
    dataset.graph(N.other).add((N.detail, XKOS.depth, Literal(999)))
    prepared = prepare_rdf(dataset)
    assert not read_classification(prepared, N.release).report.verified
    assert check_classification(
        read_classification(prepared, N.release, graph_name=N.physical)
    ).report.verified
    graph = _graph()
    member = BNode("owned")
    graph.add((N.detail, SKOS.member, member))
    prepared = prepare_rdf(graph)
    assert not read_classification(
        prepared, N.release, categories=(BNode("owned"),)
    ).report.verified
    anonymous = BNode("level")
    graph = _graph()
    for subject, predicate, value in tuple(graph):
        if N.detail in (subject, value):
            graph.remove((subject, predicate, value))
            graph.add(
                (
                    anonymous if subject == N.detail else subject,
                    predicate,
                    anonymous if value == N.detail else value,
                )
            )
    view = read_classification(prepare_rdf(graph), N.release)
    level = view.levels[1].identifier
    assert check_classification(
        view, ClassificationPolicy(required_levels=(level,))
    ).report.verified
    foreign = BNode(str(level))
    for policy in (
        ClassificationPolicy(required_levels=(foreign,)),
        ClassificationPolicy(codes=(LevelCodePolicy(foreign),)),
        ClassificationPolicy(partition=PartitionContract((foreign,), (N.unit,), True)),
    ):
        assert not check_classification(view, policy).report.verified
    graph = _graph()
    graph.add((N.detail, XKOS.notationPattern, Literal("[")))
    assert check_classification(read_classification(graph, N.release)).report.verified
    graph.set(
        (N.detail, XKOS.depth, Literal("02", datatype=XSD.integer, normalize=False))
    )
    assert check_classification(read_classification(graph, N.release)).report.verified
    head = graph.value(N.release, XKOS.levels)
    assert head is not None
    graph.set((head, RDF.rest, head))
    assert not read_classification(graph, N.release).report.verified


def test_cumulative_bounds_and_cancellation() -> None:
    """Smaller consumer quotas never accept unexecuted requirements."""
    view = read_classification(_graph(), N.release)
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=64),
    ):
        result = check_classification(view, budget=Budget(limits))
        assert not result.report.verified
        assert any(issue.kind == "exhausted" for issue in result.report.issues)
    event = Event()
    event.set()
    assert not check_classification(view, cancel=event).report.verified
    assert not read_classification(_graph(), N.release, timeout=1e-12).report.verified
