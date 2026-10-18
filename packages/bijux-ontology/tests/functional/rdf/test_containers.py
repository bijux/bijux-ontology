"""Original numbered membership stays distinct from closed application policy."""

import threading
from typing import cast

from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.containers import (
    ContainerContract,
    check_containers,
    container_to_list,
    inspect_containers,
)
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.migrations.lists.types import ListTarget, ListValue
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.reports import Issue, Report

S, P, C, NOTE, OTHER, G, H = (
    URIRef("urn:container:" + n) for n in ("s", "p", "c", "note", "other", "G", "H")
)


def _typed(graph: Graph, kind: URIRef = RDF.Seq, node: URIRef | BNode = C) -> None:
    """Author an explicit existing container edge without constructing membership."""
    graph.add((S, P, node)).add((node, RDF.type, kind))


def _check(expected: tuple[Node, ...]) -> tuple[MigrationCheck, ...]:
    """A real after-condition reads the actual materialized list with shared code."""

    def check(source: PreparedSource) -> Report:
        graph = source.graph()
        head = graph.value(S, P)
        assert head is not None
        values = tuple(iter_rdf_list(graph, head))
        return Report(
            "actual-list",
            1,
            ()
            if tuple(map(term_key, values)) == tuple(map(term_key, expected))
            else (
                Issue(
                    "container.actual_sequence",
                    "The actual output list differs from the selected sequence",
                ),
            ),
        )

    return (MigrationCheck("actual-list", check),)


def _before() -> tuple[MigrationCheck, ...]:
    """Check the original source has a concrete selected root edge."""

    def check(source: PreparedSource) -> Report:
        graph = source.graph()
        return Report(
            "original-edge",
            1,
            ()
            if any(graph.objects(S, P))
            else (Issue("container.root_missing", "The original root edge is absent"),),
        )

    return (MigrationCheck("original-edge", check),)


def test_numeric_order_exact_terms_compact_gaps_and_open_rdf() -> None:
    """An observed gap is data, not a completeness failure inferred by RDF."""
    graph = Graph()
    _typed(graph)
    value = Literal("01", datatype=XSD.integer, normalize=False)
    graph.add((C, RDF["_10"], Literal("last"))).add((C, RDF["_2"], value)).add(
        (C, RDF["_1"], value)
    )
    graph.add((C, RDF["_01"], Literal("ordinary annotation")))
    source = prepare_source(graph)
    raw = inspect_containers(source)
    assert raw.report.verified and len(raw.records) == 1
    record = raw.records[0]
    assert [slot.ordinal for slot in record.slots] == ["1", "2", "10"]
    assert record.slots[0].values[0] is value and record.slots[1].values[0] is value
    assert [(g.after, g.before) for g in record.gaps] == [("2", "10")]
    assert len(record.annotations) == 1 and not raw.report.issues
    closed = check_containers(source, ContainerContract("sequence"))
    assert closed.report.outcome == "disagreement"
    assert check_containers(
        source, ContainerContract("bag", size=3, require_type=False)
    ).report.verified
    huge = "9" * 5000
    enormous = prepare_source(Graph().add((C, RDF["_" + huge], Literal("value"))))
    observed = inspect_containers(enormous)
    assert observed.report.verified and observed.records[0].slots[0].ordinal == huge
    assert (
        len(observed.records[0].gaps) == 1 and observed.records[0].gaps[0].after is None
    )


def test_ambiguous_slots_are_retained_without_value_cartesian_products() -> None:
    """Distinct slot values are ambiguous; equal RDF aliases are one component."""
    graph = Graph()
    _typed(graph)
    graph.add((C, RDF["_1"], Literal("01", datatype=XSD.integer, normalize=False)))
    graph.add((C, RDF["_1"], Literal("1", datatype=XSD.integer, normalize=False)))
    graph.add((C, RDF["_3"], Literal("third")))
    source = prepare_source(graph)
    record = inspect_containers(source).records[0]
    assert len(record.slots[0].values) == 2
    assert {str(v) for v in record.slots[0].values} == {"01", "1"}
    assert (
        check_containers(source, ContainerContract("sequence")).report.outcome
        == "disagreement"
    )
    rejected = container_to_list(
        source, ListTarget(S, P, C), ContainerContract("sequence"), expected_values=()
    )
    assert rejected.candidate is None and rejected.report.outcome == "disagreement"
    aliases = Graph()
    _typed(aliases)
    aliases.add((C, RDF["_1"], Literal("same"))).add(
        (C, RDF["_1"], Literal("same", datatype=XSD.string))
    )
    raw = inspect_containers(prepare_source(aliases)).records[0]
    assert len(raw.slots[0].values) == 1 and len(raw.slots[0].statements) == 2
    assert check_containers(
        prepare_source(aliases), ContainerContract("sequence", size=1)
    ).report.verified


def test_list_conversion_preserves_numbered_order_duplicates_and_original_data() -> (
    None
):
    """Ten positions use numeric ordering and actual list postconditions."""
    graph = Graph()
    _typed(graph)
    graph.add((OTHER, P, C)).add((C, NOTE, Literal("container only")))
    values = tuple(
        Literal("01", datatype=XSD.integer, normalize=False)
        if n in (1, 10)
        else Literal(str(n))
        for n in range(1, 11)
    )
    for n, value in enumerate(values, 1):
        graph.add((C, RDF["_" + str(n)], value))
    source = prepare_source(graph)
    original = frozenset(source.graph())
    result = container_to_list(
        source,
        ListTarget(S, P, C),
        ContainerContract("sequence", size=10),
        expected_values=values,
        before_checks=_before(),
        after_checks=_check(values),
    )
    assert (
        result.report.verified
        and result.candidate is not None
        and result.candidate.source is not None
    )
    output = result.candidate.source.graph()
    head = output.value(S, P)
    assert head is not None
    actual = tuple(iter_rdf_list(output, head))
    assert tuple(map(term_key, actual)) == tuple(map(term_key, values))
    assert str(actual[0]) == str(actual[9]) == "01"
    assert (OTHER, P, C) in output
    output_keys = {tuple(map(term_key, triple)) for triple in output}
    assert all(
        tuple(map(term_key, triple)) in output_keys
        for triple in original
        if triple != (S, P, C)
    )
    assert frozenset(source.graph()) == original
    failed = container_to_list(
        source,
        ListTarget(S, P, C),
        ContainerContract("sequence"),
        expected_values=values,
        before_checks=_before(),
        after_checks=_check(tuple(reversed(values))),
    )
    assert failed.candidate is not None and failed.report.outcome == "disagreement"
    missing = container_to_list(
        source,
        ListTarget(S, P, C),
        ContainerContract("sequence"),
        expected_values=values,
    )
    assert missing.candidate is not None and not missing.report.verified


def test_bag_order_empty_conversion_and_explicit_rejected_expectation() -> None:
    """Bag ordering is a choice, and an empty container becomes actual rdf:nil."""
    graph = Graph()
    _typed(graph, RDF.Bag)
    graph.add((C, RDF["_1"], Literal("same"))).add((C, RDF["_3"], Literal("same")))
    source = prepare_source(graph)
    expected = (Literal("same"),) * 2
    unspecified = container_to_list(
        source, ListTarget(S, P, C), ContainerContract("bag"), expected_values=expected
    )
    assert unspecified.candidate is None and unspecified.report.outcome == "unverified"
    chosen = container_to_list(
        source,
        ListTarget(S, P, C),
        ContainerContract("bag", size=2),
        order="numbered",
        expected_values=expected,
        before_checks=_before(),
        after_checks=_check(expected),
    )
    assert chosen.report.verified
    wrong = container_to_list(
        source,
        ListTarget(S, P, C),
        ContainerContract("bag"),
        order="numbered",
        expected_values=(Literal("different"),),
    )
    assert wrong.candidate is None and wrong.report.outcome == "disagreement"
    empty = Graph()
    _typed(empty)
    owned = prepare_source(empty)
    converted = container_to_list(
        owned,
        ListTarget(S, P, C),
        ContainerContract("sequence", size=0),
        expected_values=(),
        before_checks=_before(),
        after_checks=_check(()),
    )
    assert (
        converted.report.verified
        and converted.candidate is not None
        and converted.candidate.source is not None
    )
    assert converted.candidate.source.graph().value(S, P) == RDF.nil
    assert owned.graph().value(S, P) == C


def test_physical_graphs_joint_blank_ownership_and_hidden_scope() -> None:
    """Blank members retain sharing across roles without cross-graph gap filling."""
    data = Dataset()
    container = BNode()
    member = BNode()
    _typed(data.graph(container), RDF.Bag, container)
    data.graph(container).add((container, RDF["_1"], member)).add(
        (container, RDF["_3"], member)
    )
    data.graph(H).add((member, NOTE, container))
    source = prepare_source(data)
    name = next(n for n in source._graph_names() if isinstance(n, BNode))
    node = source._read(name).value(S, P)
    assert isinstance(node, BNode)
    value = source._read(name).value(node, RDF["_1"])
    assert isinstance(value, BNode)
    foreign = inspect_containers(source, nodes=(BNode(str(node)),))
    assert foreign.report.outcome == "unverified"
    done = container_to_list(
        source,
        ListTarget(S, P, node, graph=name),
        ContainerContract("bag", size=2),
        order="numbered",
        expected_values=(value, value),
    )
    assert (
        done.candidate is not None
        and done.candidate.source is not None
        and not done.report.verified
    )
    output = done.candidate.source
    outname = next(n for n in output._graph_names() if isinstance(n, BNode))
    head = output._read(outname).value(S, P)
    assert head is not None
    members = tuple(iter_rdf_list(output._read(outname), head))
    assert len(members) == 2 and members[0] == members[1]
    assert (members[0], NOTE, outname) in output._read(H)
    split = Dataset()
    _typed(split.graph(G))
    split.graph(G).add((C, RDF["_1"], Literal("one")))
    split.graph(H).add((C, RDF["_2"], Literal("two")))
    raw = inspect_containers(prepare_source(split), nodes=(C,))
    assert len(raw.records) == 2 and all(len(r.slots) == 1 for r in raw.records)
    pinned = prepare_source(RdfSource(split, graph_name=str(G)))
    assert {r.graph for r in inspect_containers(pinned).records} == {G}
    assert (
        check_containers(pinned, ContainerContract("sequence", size=2)).report.outcome
        == "disagreement"
    )


def test_limits_cancellation_empty_scope_and_paid_finding_prefix() -> None:
    """Bounded incomplete reads never become a closed application certificate."""
    graph = Graph().add((C, RDF["_2"], Literal("a"))).add((C, RDF["_2"], Literal("b")))
    source = prepare_source(graph)
    stopped = check_containers(
        source, ContainerContract("sequence"), limits=Limits(max_issues=1)
    )
    assert stopped.report.outcome == "exhausted" and any(
        i.kind == "finding" for i in stopped.report.issues
    )
    assert (
        check_containers(source, ContainerContract("sequence"), nodes=()).report.outcome
        == "unverified"
    )
    assert (
        check_containers(
            source, ContainerContract("sequence"), nodes=(OTHER,)
        ).report.outcome
        == "disagreement"
    )
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=16)):
        cut = inspect_containers(source, limits=limits)
        assert cut.report.outcome == "exhausted"
    budget = Budget()
    assert inspect_containers(source, budget=budget).report.verified
    budget.consume("work", budget.remaining("work"))
    assert (
        check_containers(
            source, ContainerContract("sequence"), budget=budget
        ).report.outcome
        == "exhausted"
    )
    _typed(graph)
    valid = prepare_source(graph)
    cancel = threading.Event()
    cancel.set()
    result = container_to_list(
        valid,
        ListTarget(S, P, C),
        ContainerContract("sequence"),
        expected_values=cast(tuple[ListValue, ...], ()),
        cancel=cancel,
    )
    assert result.candidate is None and result.report.outcome == "exhausted"
