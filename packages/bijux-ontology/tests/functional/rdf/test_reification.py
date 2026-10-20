"""Exact reifier identity and explicit application rules never assert base facts."""

import sys
from types import FrameType
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.reification.checking import check_reifications
from bijux_ontology.rdf.reification.reading import inspect_reifications
from bijux_ontology.rdf.reification.types import ReificationContract, ReificationRecord
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource

S, P, R, OTHER, NOTE, G, H = (
    URIRef("urn:reification:" + part)
    for part in ("s", "p", "record", "other", "note", "g", "h")
)
STRICT = ReificationContract(require_type=True)


def _add(graph: Graph, node: URIRef | BNode, value: Literal) -> None:
    """Author ordinary exact RDF component assertions through RDFLib."""
    graph.add((node, RDF.type, RDF.Statement))
    graph.add((node, RDF.subject, S))
    graph.add((node, RDF.predicate, P))
    graph.add((node, RDF.object, value))


def test_literal_and_attached_annotations_do_not_assert_the_statement() -> None:
    graph = Graph()
    value = Literal("01", datatype=XSD.integer, normalize=False)
    _add(graph, R, value)
    graph.add((R, NOTE, Literal("Exact words", lang="en-GB")))
    attachment = BNode()
    graph.add((R, OTHER, attachment))
    graph.add((attachment, RDFS.comment, Literal("untraversed body")))
    source = prepare_source(graph)
    before = frozenset(source.graph())
    raw = inspect_reifications(source)
    assert raw.report.verified and raw.source is source and len(raw.records) == 1
    record = raw.records[0]
    assert record.statement == (S, P, value) and record.objects[0] is value
    assert record.status == "complete" and record.explicit_type
    assert len(record.annotations) == 2 and len(record.statements) == 6
    assert record.annotations[0] in source.graph()
    assert (S, P, value) not in source.graph()
    checked = check_reifications(source, STRICT)
    assert checked.report.verified and checked.contract is STRICT
    assert frozenset(source.graph()) == before
    assert (S, P, value) not in source.graph()


def test_malformed_multiple_and_invalid_roles_are_retained_before_strict_failure() -> (
    None
):
    graph = Graph()
    graph.add((R, RDF.subject, S))
    graph.add((R, RDF.predicate, P))
    graph.add((OTHER, RDF.subject, S))
    graph.add((OTHER, RDF.predicate, P))
    graph.add((OTHER, RDF.object, Literal("01", datatype=XSD.integer, normalize=False)))
    graph.add((OTHER, RDF.object, Literal("1", datatype=XSD.integer, normalize=False)))
    bad = URIRef("urn:reification:bad")
    graph.add((bad, RDF.subject, Literal("not a resource")))
    graph.add((bad, RDF.predicate, BNode()))
    graph.add((bad, RDF.object, S))
    source = prepare_source(graph)
    raw = inspect_reifications(source)
    assert raw.report.verified and len(raw.records) == 3
    records = {record.node: record for record in raw.records}
    assert records[R].status == "incomplete" and records[R].objects == ()
    assert records[OTHER].status == "ambiguous" and len(records[OTHER].objects) == 2
    assert records[bad].status == "invalid_roles"
    assert all(record.statement is None for record in raw.records)
    checked = check_reifications(source, STRICT)
    assert checked.report.outcome == "disagreement"
    assert checked.inspection.records == raw.records
    assert len(checked.report.issues) >= 3
    # Explicitly dropping component uniqueness only relaxes that application rule.
    loose = check_reifications(
        source, ReificationContract(unique_components=False), nodes=(OTHER,)
    )
    assert loose.report.verified and loose.inspection.records[0].statement is None
    assert "reification.component_uniqueness" in loose.report.exclusions


def test_distinct_reifiers_do_not_merge_or_inherit_the_others_annotations() -> None:
    graph = Graph()
    _add(graph, R, Literal("same"))
    _add(graph, OTHER, Literal("same", datatype=XSD.string))
    graph.add((R, NOTE, Literal("only first")))
    source = prepare_source(graph)
    raw = inspect_reifications(source)
    assert len(raw.records) == 2
    by_node = {record.node: record for record in raw.records}
    assert len(by_node[R].annotations) == 1 and not by_node[OTHER].annotations
    assert check_reifications(source, STRICT).report.verified
    unique = check_reifications(source, ReificationContract(unique_statements=True))
    assert unique.report.outcome == "disagreement"
    # Intrinsic plain/xsd:string aliases on one record are one RDF component.
    graph.add((R, RDF.object, Literal("same", datatype=XSD.string)))
    aliases = inspect_reifications(prepare_source(graph), nodes=(R,))
    assert aliases.report.verified and aliases.records[0].status == "complete"
    assert len(aliases.records[0].objects) == 1
    assert len([s for s in aliases.records[0].statements if s[1] == RDF.object]) == 2


def test_physical_graphs_and_exact_blank_selector_ownership() -> None:
    data = Dataset()
    data.graph(G).add((R, RDF.subject, S))
    data.graph(G).add((R, RDF.predicate, P))
    data.graph(H).add((R, RDF.object, Literal("split")))
    blank = BNode()
    _add(data.graph(blank), blank, Literal("owned"))
    source = prepare_source(data)
    raw = inspect_reifications(source)
    split = [record for record in raw.records if record.node == R]
    assert len(split) == 2 and all(record.statement is None for record in split)
    assert {record.graph for record in split} == {G, H}
    assert (
        check_reifications(source, STRICT, nodes=(R,)).report.outcome == "disagreement"
    )
    owned = next(name for name in source._graph_names() if isinstance(name, BNode))
    selected = check_reifications(source, STRICT, nodes=(owned,), graph_name=owned)
    assert selected.report.verified and selected.inspection.records[0].graph is owned
    foreign = BNode(str(owned))
    assert (
        inspect_reifications(source, graph_name=foreign).report.outcome == "unverified"
    )
    assert inspect_reifications(source, nodes=(foreign,)).report.outcome == "unverified"
    assert (
        inspect_reifications(source, graph_name=Literal(str(G))).report.outcome
        == "input_error"
    )
    union = prepare_source(RdfSource(data, union=True))
    unknown = check_reifications(union, ReificationContract(require_complete=False))
    assert not unknown.report.verified and not unknown.inspection.records
    pinned = prepare_source(RdfSource(data, graph_name=str(G)))
    assert {record.graph for record in inspect_reifications(pinned).records} == {G}


def test_missing_selection_empty_inventory_and_explicit_type_obligation() -> None:
    source = prepare_source(Graph().add((R, RDFS.label, Literal("ordinary node"))))
    inspected = inspect_reifications(source)
    assert not inspected.records and inspected.report.complete
    assert check_reifications(source, STRICT).report.outcome == "unverified"
    chosen = check_reifications(source, STRICT, nodes=(R,))
    assert chosen.report.outcome == "disagreement" and chosen.inspection.records
    missing = check_reifications(source, STRICT, nodes=(OTHER,))
    assert missing.inspection.missing == (OTHER,) and not missing.inspection.records
    assert missing.report.outcome == "disagreement"
    assert (
        check_reifications(
            source, ReificationContract(require_complete=False), nodes=(OTHER,)
        ).report.outcome
        == "unverified"
    )
    assert (
        inspect_reifications(
            source, nodes=(cast(URIRef, Literal(str(R))),)
        ).report.outcome
        == "input_error"
    )
    graph = Graph()
    _add(graph, R, Literal("v"))
    graph.remove((R, RDF.type, RDF.Statement))
    untyped = prepare_source(graph)
    assert check_reifications(untyped, ReificationContract()).report.verified
    assert check_reifications(untyped, STRICT).report.outcome == "disagreement"
    with pytest.raises(InputError):
        ReificationContract(False, False, False, False)


def test_limits_preserve_records_and_findings_without_false_acceptance() -> None:
    graph = Graph().add((R, RDF.subject, S)).add((R, RDF.predicate, P))
    source = prepare_source(graph)
    bounded = check_reifications(source, STRICT, limits=Limits(max_issues=1))
    assert bounded.report.outcome == "exhausted"
    assert any(
        issue.code == "rdf.reification_contract" for issue in bounded.report.issues
    )
    assert len(bounded.inspection.records) == 1
    for limits in (Limits(max_work=1), Limits(max_states=1), Limits(max_triples=1)):
        stopped = check_reifications(
            source, ReificationContract(require_complete=False), limits=limits
        )
        assert not stopped.report.verified and stopped.report.outcome == "exhausted"
    oversized = prepare_source(Graph().add((R, RDF.object, Literal("x" * 10000))))
    result = inspect_reifications(oversized, limits=Limits(max_bytes=1000))
    assert result.report.outcome == "exhausted" and not result.records
    partial = PreparedSource(
        source.dataset(),
        is_dataset=False,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
        graph_fidelity="nonempty-only",
    )
    assert not check_reifications(partial, ReificationContract()).report.verified
    budget = Budget()
    assert inspect_reifications(source, budget=budget).report.verified
    budget.consume("work", budget.remaining("work"))
    assert (
        check_reifications(source, STRICT, budget=budget).report.outcome == "exhausted"
    )
    assert check_reifications(source, STRICT).report.outcome == "disagreement"


def test_completed_raw_record_has_no_late_output_reservation() -> None:
    """Every actual completed helper return survives a later state exhaustion."""
    graph = Graph()
    _add(graph, R, Literal("observed"))
    source = prepare_source(graph)
    measured = Budget()
    assert inspect_reifications(source, nodes=(R,), budget=measured).report.verified
    used = measured.limits.max_states - measured.remaining("states")
    returned: list[ReificationRecord] = []

    def observe(frame: FrameType, event: str, value: object) -> None:
        if (
            event == "return"
            and frame.f_code.co_name == "_record"
            and isinstance(value, ReificationRecord)
        ):
            returned.append(value)

    for cap in (used - 1, used):
        returned.clear()
        previous = sys.getprofile()
        sys.setprofile(observe)
        try:
            result = inspect_reifications(
                source, nodes=(R,), limits=Limits(max_states=cap)
            )
        finally:
            sys.setprofile(previous)
        assert all(
            any(record is published for published in result.records)
            for record in returned
        )
        assert result.report.outcome == ("agreement" if cap == used else "exhausted")
