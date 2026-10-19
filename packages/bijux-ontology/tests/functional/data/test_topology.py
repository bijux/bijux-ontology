"""Caller record topology remains distinct from OWL and ordinary references."""

from collections.abc import Sequence
from typing import Literal as Choice
from typing import cast

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from bijux_ontology.data.topology import TopologyRecord, check_record_topology
from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Location
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("urn:topology:")


def _graph(*nodes: URIRef) -> Graph:
    """Describe actual records without assigning ontology-level containment."""
    result = Graph()
    for node in nodes:
        result.add((node, RDF.type, EX.Record))
    return result


def _records(*nodes: URIRef) -> tuple[TopologyRecord, ...]:
    """Supply known diagnostic locations independently of RDF identity."""
    return tuple(
        TopologyRecord(
            str(node), node, location=Location("output.json", ("@graph", index))
        )
        for index, node in enumerate(nodes)
    )


def _contract(
    *, min_owners: int | None = None, max_owners: int | None = None
) -> TopologyContract:
    """Constrain authored containment while leaving reference cycles legal."""
    return TopologyContract(
        (
            TopologyRelation("contains", EX.contains, ownership=True, acyclic=True),
            TopologyRelation("reference", EX.reference),
        ),
        min_owners=min_owners,
        max_owners=max_owners,
    )


def test_roots_orphans_owners_shared_targets_and_reference_cycles() -> None:
    graph = _graph(EX.root, EX.a, EX.b, EX.shared)
    graph.add((EX.root, EX.contains, EX.a)).add((EX.root, EX.contains, EX.b))
    graph.add((EX.a, EX.contains, EX.shared)).add((EX.b, EX.contains, EX.shared))
    graph.add((EX.shared, EX.reference, EX.a))
    records = _records(EX.root, EX.a, EX.b, EX.shared)
    contract = _contract(min_owners=1, max_owners=1)
    result = check_record_topology(
        prepare_rdf(graph),
        records,
        contract,
        roots=(str(EX.root),),
        shared_targets=(str(EX.shared),),
        complete=True,
        closure="closed",
    )
    assert result.verified
    unshared = check_record_topology(
        prepare_rdf(graph),
        records,
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    )
    assert any(
        i.code == "data.topology_owners" and i.actual == "2" for i in unshared.issues
    )
    assert not any(i.code == "data.topology_cycle" for i in unshared.issues)
    graph.add((EX.shared, EX.contains, EX.root))
    graph.add((EX.orphan, RDF.type, EX.Record))
    result = check_record_topology(
        prepare_rdf(graph),
        (*records, _records(EX.orphan)[0]),
        contract,
        roots=(str(EX.root),),
        shared_targets=(str(EX.shared),),
        complete=True,
        closure="closed",
    )
    cycle = next(i for i in result.issues if i.code == "data.topology_cycle")
    assert cycle.details is not None and cycle.evidence is not None
    assert any(
        i.code == "data.topology_unreachable"
        and i.details is not None
        and i.details.term is None
        for i in result.issues
    )
    # Path aliases never turn one owner into two owner records.
    graph = _graph(EX.root, EX.a).add((EX.root, EX.contains, EX.a))
    aliases = TopologyContract(
        (
            TopologyRelation("one", EX.contains, ownership=True),
            TopologyRelation("alias", EX.contains, ownership=True),
        ),
        min_owners=1,
        max_owners=1,
    )
    assert check_record_topology(
        prepare_rdf(graph),
        _records(EX.root, EX.a),
        aliases,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    ).verified


def test_open_partial_external_scope_and_required_empty_roots() -> None:
    graph = _graph(EX.root, EX.a).add((EX.root, EX.contains, EX.external))
    contract = _contract(min_owners=1, max_owners=1)
    scopes: tuple[tuple[bool, Choice["open", "closed"]], ...] = (
        (True, "open"),
        (False, "closed"),
    )
    for complete, closure in scopes:
        result = check_record_topology(
            prepare_rdf(graph),
            _records(EX.root, EX.a),
            contract,
            roots=(str(EX.root),),
            complete=complete,
            closure=closure,
        )
        assert result.outcome == "unverified" and all(
            i.kind != "finding" for i in result.issues
        )
    closed = check_record_topology(
        prepare_rdf(graph),
        _records(EX.root, EX.a),
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    )
    assert any(i.code == "data.topology_reference_missing" for i in closed.issues)
    empty_roots = check_record_topology(
        prepare_rdf(_graph(EX.root)),
        _records(EX.root),
        contract,
        roots=(),
        complete=True,
        closure="closed",
    )
    assert not empty_roots.verified and any(
        i.code == "data.topology_roots_unverified" for i in empty_roots.issues
    )
    assert not check_record_topology(
        prepare_rdf(Graph()), (), contract, roots=(), complete=True, closure="closed"
    ).verified
    missing = check_record_topology(
        prepare_rdf(Graph()),
        _records(EX.root),
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    )
    assert not missing.verified and any(
        i.code == "data.topology_record_missing" for i in missing.issues
    )
    with pytest.raises(InputError):
        check_record_topology(
            prepare_rdf(graph),
            _records(EX.root),
            contract,
            roots=("unknown-record",),
            complete=True,
            closure="closed",
        )


def test_graph_scope_owned_blank_bindings_and_immutable_inputs() -> None:
    data = Dataset()
    for triple in _graph(EX.root, EX.a, EX.b):
        data.graph(EX.primary).add(triple)
    data.graph(EX.primary).add((EX.root, EX.contains, EX.a))
    data.graph(EX.support).add((EX.a, EX.contains, EX.b))
    records = _records(EX.root, EX.a, EX.b)
    contract = _contract(min_owners=1, max_owners=1)
    selected = prepare_rdf(RdfSource(data, graph_name=str(EX.primary)))
    assert not check_record_topology(
        selected,
        records,
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    ).verified
    effective = prepare_rdf(RdfSource(data, union=True))
    data.remove_graph(EX.primary)
    data.remove_graph(EX.support)
    assert check_record_topology(
        effective,
        records,
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    ).verified
    blank_source = prepare_rdf(Graph().add((BNode(), RDF.type, EX.Record)))
    blank = next(blank_source.graph().subjects())
    assert isinstance(blank, BNode)
    assert check_record_topology(
        blank_source,
        (TopologyRecord("blank", blank),),
        contract,
        roots=("blank",),
        complete=True,
        closure="closed",
    ).verified
    foreign = check_record_topology(
        blank_source,
        (TopologyRecord("blank", BNode(str(blank))),),
        contract,
        roots=("blank",),
        complete=True,
        closure="closed",
    )
    assert foreign.outcome == "unverified"
    with pytest.raises(InputError):
        check_record_topology(
            effective,
            (records[0], TopologyRecord(URIRef(records[0].identifier), EX.a)),
            contract,
            roots=(str(EX.root),),
            complete=True,
            closure="closed",
        )
    named = Dataset()
    named.graph(BNode()).add((EX.root, RDF.type, EX.Record))
    source = PreparedSource(
        named,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    name = next(
        g.identifier
        for g in source.dataset().graphs()
        if isinstance(g.identifier, BNode)
    )
    assert check_record_topology(
        source,
        _records(EX.root),
        contract,
        roots=(str(EX.root),),
        graph_name=name,
        complete=True,
        closure="closed",
    ).verified
    assert (
        check_record_topology(
            source,
            _records(EX.root),
            contract,
            roots=(str(EX.root),),
            graph_name=BNode(str(name)),
            complete=True,
            closure="closed",
        ).outcome
        == "unverified"
    )


def test_finite_paths_rdf_identity_and_lossy_intermediate_scope() -> None:
    graph = (
        _graph(EX.root, EX.a)
        .add((EX.root, EX.p, Literal("alpha")))
        .add((EX.left, EX.q, Literal("alpha", datatype=XSD.string)))
        .add((EX.left, EX.child, EX.a))
    )
    contract = TopologyContract(
        (
            TopologyRelation(
                "path", EX.p / ~EX.q / EX.child, ownership=True, acyclic=True
            ),
        ),
        min_owners=1,
        max_owners=1,
    )
    assert check_record_topology(
        prepare_rdf(graph),
        _records(EX.root, EX.a),
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    ).verified
    unsupported = TopologyContract((TopologyRelation("choice", EX.p | EX.q),))
    result = check_record_topology(
        prepare_rdf(graph),
        _records(EX.root, EX.a),
        unsupported,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    )
    assert result.outcome == "unverified" and all(
        i.kind != "finding" for i in result.issues
    )
    payload = (
        b"@prefix e:<urn:topology:> . "
        b"@prefix xsd:<http://www.w3.org/2001/XMLSchema#> . "
        b'e:root e:p "  alpha  "^^xsd:token . '
        b'e:left e:q "alpha"^^xsd:token; e:child e:a . e:a a e:Record .'
    )
    lossy = prepare_rdf(RdfSource(payload, format="turtle"), preserve_literals=False)
    result = check_record_topology(
        lossy,
        _records(EX.root, EX.a),
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    )
    assert result.outcome == "unverified" and all(
        i.kind != "finding" for i in result.issues
    )
    resource_only = prepare_rdf(
        RdfSource(
            b"<urn:topology:root> <urn:topology:contains> <urn:topology:a> .",
            format="turtle",
        ),
        preserve_literals=False,
    )
    assert check_record_topology(
        resource_only,
        _records(EX.root, EX.a),
        _contract(),
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
    ).verified


def test_findings_survive_later_bounds_and_metadata_is_bounded() -> None:
    graph = (
        _graph(EX.root, EX.a, EX.b)
        .add((EX.root, EX.contains, EX.a))
        .add((EX.a, EX.contains, EX.root))
        .add((EX.b, EX.contains, EX.a))
    )
    records = _records(EX.root, EX.a, EX.b)
    contract = _contract(max_owners=1)
    result = check_record_topology(
        prepare_rdf(graph),
        records,
        contract,
        roots=(str(EX.root),),
        complete=True,
        closure="closed",
        limits=Limits(max_issues=1),
    )
    assert result.outcome == "exhausted" and any(
        i.code == "data.topology_cycle" for i in result.issues
    )
    partial = check_record_topology(
        prepare_rdf(graph),
        records,
        contract,
        roots=(str(EX.root),),
        complete=False,
        closure="open",
    )
    assert any(i.kind == "finding" for i in partial.issues) and not partial.complete
    huge = TopologyRecord(
        "root", EX.root, location=Location("output.json", ("x" * 100000,))
    )
    result = check_record_topology(
        prepare_rdf(graph),
        (huge,),
        contract,
        roots=("root",),
        complete=True,
        closure="closed",
        limits=Limits(max_bytes=1000),
    )
    assert result.outcome == "exhausted" and all(
        len(i.location) <= 1000 for i in result.issues
    )
    with pytest.raises(InputError):
        check_record_topology(
            prepare_rdf(graph),
            cast(Sequence[TopologyRecord], iter(records)),
            contract,
            roots=(str(EX.root),),
            complete=True,
            closure="closed",
        )
    with pytest.raises(InputError):
        TopologyContract(contract.relations, min_owners=2, max_owners=1)


def test_capture_count_and_unicode_diagnostics_are_bounded() -> None:
    class Changing(list[TopologyRecord]):
        def __init__(self, first: int) -> None:
            super().__init__(_records(EX.root, EX.a))
            self.first = first
            self.reads = 0

        def __len__(self) -> int:
            self.reads += 1
            return self.first if self.reads == 1 else 3 - self.first

    source = prepare_rdf(_graph(EX.root, EX.a).add((EX.root, EX.contains, EX.a)))
    for first in (1, 2):
        records = Changing(first)
        report = check_record_topology(
            source,
            records,
            _contract(),
            roots=(str(EX.root),),
            complete=True,
            closure="closed",
        )
        assert records.reads == 1 and report.requested == first
        assert report.verified == (first == 2)
    unicode_location = TopologyRecord(
        "root", EX.root, location=Location("é" * 250, ("é" * 250,))
    )
    report = check_record_topology(
        prepare_rdf(Graph()),
        (unicode_location,),
        _contract(),
        roots=("root",),
        complete=True,
        closure="closed",
        limits=Limits(max_bytes=1000),
    )
    assert report.outcome == "exhausted" and all(
        len(issue.location.encode("utf-8")) <= 1000 for issue in report.issues
    )
    report = check_record_topology(
        source,
        (TopologyRecord("😀" * 1000, EX.root),),
        _contract(),
        roots=("😀" * 1000,),
        complete=True,
        closure="closed",
        limits=Limits(max_bytes=22000),
    )
    assert report.outcome == "exhausted" and all(
        len((issue.evidence_json or "").encode("utf-8")) <= 22000
        for issue in report.issues
    )
