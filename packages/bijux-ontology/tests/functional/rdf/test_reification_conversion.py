"""Actual native statement conversion retains reifiers, graph scope and truth."""

from importlib.metadata import version
from threading import Event
from typing import cast

import pyoxigraph as ox
import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.reification.conversion import (
    REIFIER_CONTRACT,
    ReifierDataset,
    ReifierSelection,
    convert_reifications,
    restore_reifications,
)
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.triple_terms import (
    TRIPLE_TERM_CONTRACT,
    prepare_triple_terms,
)

E = Namespace("urn:statement-conversion:")
REIFIES = str(RDF) + "reifies"


def _contract() -> str:
    """Explicitly select the actually installed qualified native profile."""
    return "rdf12-cr-20260407-reifiers-pyoxigraph-" + version("pyoxigraph")


def _record(graph: Graph, node: URIRef | BNode, subject: URIRef | BNode = E.s) -> None:
    """Author a complete typed exact record without its described assertion."""
    for predicate, value in (
        (RDF.type, RDF.Statement),
        (RDF.subject, subject),
        (RDF.predicate, E.p),
        (RDF.object, Literal("01", datatype=XSD.integer, normalize=False)),
    ):
        graph.add((node, predicate, value))


def test_two_reifiers_named_graph_and_annotations_round_trip_without_assertion() -> (
    None
):
    dataset = Dataset(default_union=False)
    graph = dataset.graph(E.graph)
    _record(graph, E.first)
    _record(graph, E.second)
    graph.add((E.first, E.note, Literal("first\r\nannotation ", lang="en")))
    graph.add((E.second, E.note, Literal("second")))
    graph.add((E.ordinary, E.note, Literal("untouched")))
    dataset.graph(E.empty)
    source = prepare_rdf(dataset)
    before = frozenset(source.graph(E.graph))
    result = convert_reifications(
        source,
        (ReifierSelection(E.first, E.graph), ReifierSelection(E.second, E.graph)),
        contract=_contract(),
    )
    assert result.report.verified, result.report.to_dict()
    snapshot = result.snapshot
    assert snapshot is not None and snapshot.contract == _contract()
    reifying = [q for q in snapshot.quads if q.predicate.value == REIFIES]
    assert len(reifying) == 2 and reifying[0].subject != reifying[1].subject
    assert reifying[0].object == reifying[1].object
    assert all(
        isinstance(q.object, ox.Triple)
        and isinstance(q.object.object, ox.Literal)
        and q.object.object.value == "01"
        for q in reifying
    )
    assert not any(
        q.subject == ox.NamedNode(str(E.s)) and q.predicate == ox.NamedNode(str(E.p))
        for q in snapshot.quads
    )
    assert ox.NamedNode(str(E.empty)) in snapshot.graphs
    assert len(snapshot.quads) == 5
    restored = restore_reifications(snapshot)
    assert restored.report.verified and restored.source is not None, (
        restored.report.to_dict()
    )
    assert compare_rdf(source, restored.source).verified
    assert frozenset(source.graph(E.graph)) == before
    assert (
        E.s,
        E.p,
        Literal("01", datatype=XSD.integer, normalize=False),
    ) not in restored.source.graph(E.graph)


def test_blank_graph_sharing_and_separately_asserted_statement_are_preserved() -> None:
    dataset = Dataset(default_union=False)
    name, subject, reifier = BNode(), BNode(), BNode()
    graph = dataset.graph(name)
    _record(graph, reifier, subject)
    graph.add((subject, E.p, Literal("01", datatype=XSD.integer, normalize=False)))
    graph.add((reifier, E.part, subject))
    dataset.default_graph.add((E.root, E.part, subject))
    dataset.graph(E.empty)
    source = prepare_rdf(dataset)
    owned_name = next(
        g.identifier
        for g in source.dataset().graphs()
        if isinstance(g.identifier, BNode)
    )
    owned_reifier = next(source.graph(owned_name).subjects(RDF.type, RDF.Statement))
    assert isinstance(owned_reifier, BNode)
    result = convert_reifications(
        source, (ReifierSelection(owned_reifier, owned_name),), contract=_contract()
    )
    assert result.report.verified, result.report.to_dict()
    snapshot = result.snapshot
    assert snapshot is not None and len(snapshot.blanks) == 3
    actual = next(q for q in snapshot.quads if q.predicate.value == REIFIES)
    assert isinstance(actual.object, ox.Triple)
    assert any(
        q.subject == actual.object.subject
        and q.predicate == actual.object.predicate
        and q.object == actual.object.object
        and q.graph_name == actual.graph_name
        for q in snapshot.quads
    )
    assert any(
        q.subject == ox.NamedNode(str(E.root)) and q.object == actual.object.subject
        for q in snapshot.quads
    )
    restored = restore_reifications(snapshot)
    assert restored.report.verified and restored.source is not None, (
        restored.report.to_dict()
    )
    assert compare_rdf(source, restored.source).verified
    foreign = convert_reifications(
        source,
        (ReifierSelection(BNode(str(owned_reifier)), owned_name),),
        contract=_contract(),
    )
    assert foreign.snapshot is None and foreign.report.outcome == "unverified"
    wrong_graph = convert_reifications(
        source,
        (ReifierSelection(owned_reifier, BNode(str(owned_name))),),
        contract=_contract(),
    )
    assert wrong_graph.snapshot is None and not wrong_graph.report.verified


def test_only_selected_physical_records_are_checked_and_malformed_records_block() -> (
    None
):
    dataset = Dataset(default_union=False)
    _record(dataset.graph(E.good), E.first)
    _record(dataset.graph(E.bad), E.first)
    dataset.graph(E.bad).add((E.first, RDF.object, Literal("other")))
    source = prepare_rdf(dataset)
    good = convert_reifications(
        source, (ReifierSelection(E.first, E.good),), contract=_contract()
    )
    assert good.report.verified and good.snapshot is not None, good.report.to_dict()
    restored = restore_reifications(good.snapshot)
    assert restored.source is not None and compare_rdf(source, restored.source).verified
    bad = convert_reifications(
        source, (ReifierSelection(E.first, E.bad),), contract=_contract()
    )
    assert bad.snapshot is None and any(
        i.code == "rdf.reification_contract" for i in bad.report.issues
    )
    missing = convert_reifications(
        source, (ReifierSelection(E.absent, E.good),), contract=_contract()
    )
    assert missing.snapshot is None and not missing.report.verified
    mixed = Graph()
    _record(mixed, E.first)
    mixed.add((E.first, URIRef(REIFIES), E.unrelated))
    assert (
        convert_reifications(
            prepare_rdf(mixed), (ReifierSelection(E.first),), contract=_contract()
        ).snapshot
        is None
    )


def test_explicit_profiles_ownership_and_cumulative_limits_cannot_pass_empty() -> None:
    graph = Graph()
    _record(graph, E.first)
    source = prepare_rdf(graph)
    selection = (ReifierSelection(E.first),)
    empty = convert_reifications(source, (), contract=_contract())
    assert not empty.report.verified and empty.snapshot is None
    unsupported = convert_reifications(source, selection, contract="unavailable")
    assert unsupported.report.outcome == "unverified" and unsupported.snapshot is None
    other = "rdf12-cr-20260407-reifiers-pyoxigraph-" + (
        "0.5.11" if version("pyoxigraph") == "0.5.12" else "0.5.12"
    )
    assert convert_reifications(source, selection, contract=other).snapshot is None
    dataset = Dataset(default_union=False)
    dataset.default_graph.add((E.s, E.p, E.value))
    union = prepare_rdf(RdfSource(dataset, union=True))
    assert not convert_reifications(
        union, selection, contract=_contract()
    ).report.verified
    stopped = convert_reifications(
        source, selection, contract=_contract(), budget=Budget(Limits(max_work=100))
    )
    assert stopped.snapshot is None and stopped.report.outcome == "exhausted"
    cancelled = Event()
    cancelled.set()
    assert (
        convert_reifications(
            source, selection, contract=_contract(), cancel=cancelled
        ).report.outcome
        == "exhausted"
    )
    with pytest.raises(InputError):
        ReifierDataset()
    if version("pyoxigraph") == "0.5.11":
        nested = prepare_triple_terms(
            RdfSource(
                "<urn:r> <http://www.w3.org/1999/02/22-rdf-syntax-ns#reifies> "
                '<<( <urn:s> <urn:p> <<( <urn:t> <urn:q> "nested" )>> )>> .',
                format="nt",
            ),
            contract=TRIPLE_TERM_CONTRACT,
        )
        assert nested.snapshot is not None and nested.report.verified
        with pytest.raises(InputError, match="owned ReifierDataset"):
            restore_reifications(cast(ReifierDataset, nested.snapshot))
    assert REIFIER_CONTRACT.endswith("0.5.12")
