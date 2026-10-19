"""Real local allocation commits survive restart, stale writers and rejected checks."""

import json
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.authoring.allocations import (
    AllocationPolicy,
    AllocationRequest,
    AllocationStore,
    CuratorRange,
    allocation_draft_data,
    prepare_allocations,
    read_allocation_draft,
    reconcile_allocations,
)
from bijux_ontology.vocabulary.registry import IdentifierRegistry, NamespaceContribution

E = Namespace("urn:allocated:")
ALT = URIRef("http://www.geneontology.org/formats/oboInOwl#hasAlternativeId")


def _policy() -> AllocationPolicy:
    """Two curators explicitly own disjoint parts of one registered namespace."""
    return AllocationPolicy(
        IdentifierRegistry(
            (
                NamespaceContribution(str(E), "LOC", "alice", ("L",)),
                NamespaceContribution(str(E), "LOC", "bob"),
            )
        ),
        (CuratorRange("alice", "LOC", 1, 9, 3), CuratorRange("bob", "LOC", 20, 29, 3)),
        reserved=(E["002"],),
        retired=(E["003"],),
    )


def _source() -> PreparedSource:
    """An existing class, alternate ID and shared exact literal block reuse."""
    child = BNode()
    graph = Graph()
    graph.add((E["001"], RDF.type, OWL.Class))
    graph.add((E["001"], RDFS.label, Literal("Existing", lang="en")))
    graph.add((E.other, ALT, Literal("L:004")))
    graph.add((E.other, E.detail, child))
    graph.add((E.another, E.detail, child))
    graph.add((child, E.value, literal_term("01", datatype=str(XSD.integer))))
    return prepare_rdf(graph)


def _labels(source: PreparedSource) -> Report:
    """Actually inspect native declarations and required labels, without mocks."""
    graph = source.graph()
    missing = []
    checked = 0
    for subject, kind in graph.subject_objects(RDF.type):
        if kind not in (OWL.Class, SKOS.Concept, SKOS.ConceptScheme):
            continue
        checked += 1
        predicate = RDFS.label if kind == OWL.Class else SKOS.prefLabel
        if not any(graph.objects(subject, predicate)):
            missing.append(
                Issue(
                    "allocation.label_missing", "Required label is absent", str(subject)
                )
            )
    return Report("required_labels", checked, tuple(missing), requested=max(1, checked))


def test_sequential_batches_restart_and_exact_native_content(tmp_path: Path) -> None:
    """Fresh processes skip all collision inventories and preserve RDF identity."""
    path = tmp_path / "allocation.sqlite"
    store = AllocationStore(path, _policy())
    original = _source()
    first = store.create(original)
    proposal = prepare_allocations(
        store,
        first,
        (
            AllocationRequest(
                "first", label=literal_term("01", datatype=str(XSD.integer))
            ),
        ),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert proposal.report.verified and proposal.draft is not None
    assert proposal.draft.assignments[0].iri == E["005"]
    assert store.commit(proposal).revision is None
    committed = store.commit(proposal, accept=True)
    assert committed.report.verified and committed.revision == 1
    assert (E["005"], RDF.type, OWL.Class) not in original.graph()
    after = store.snapshot()
    assert str(after.source.graph().value(E["005"], RDFS.label)) == "01"
    assert after.source.graph().value(E.other, E.detail) == after.source.graph().value(
        E.another, E.detail
    )
    script = """
import sys,json
from pathlib import Path
from rdflib import Literal,Namespace
from rdflib.namespace import RDF,OWL
from bijux_ontology.vocabulary.registry import IdentifierRegistry,NamespaceContribution
from bijux_ontology.vocabulary.authoring.allocations import (
 AllocationPolicy,CuratorRange,AllocationStore,AllocationRequest,prepare_allocations
)
from bijux_ontology.rdf.migrations import MigrationCheck
from bijux_ontology.reports import Report
E=Namespace('urn:allocated:')
p=AllocationPolicy(IdentifierRegistry((NamespaceContribution(str(E),'LOC','alice',('L',)),NamespaceContribution(str(E),'LOC','bob'))),(CuratorRange('alice','LOC',1,9,3),CuratorRange('bob','LOC',20,29,3)),reserved=(E['002'],),retired=(E['003'],))
s=AllocationStore(Path(sys.argv[1]),p)
def checked(source):
 g=source.graph()
 count=len(set(g.subjects(RDF.type,OWL.Class)))
 return Report('classes',checked=count,requested=count)
r=prepare_allocations(s,s.snapshot(),(AllocationRequest('second',label=Literal('Second')),),owner='alice',prefix='LOC',after_checks=(MigrationCheck('native',checked),))
c=s.commit(r,accept=True)
assert r.draft is not None and c.report.verified
print(json.dumps({'iri':str(r.draft.assignments[0].iri),'revision':c.revision}))
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", script, str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(process.stdout) == {"iri": str(E["006"]), "revision": 2}
    assert len(store.snapshot().history) == 2


def test_owned_ranges_rejections_postchecks_and_review_conflicts(
    tmp_path: Path,
) -> None:
    """No failed range/check/scope mutates state; review collisions stay explicit."""
    store = AllocationStore(tmp_path / "ontology.sqlite", _policy())
    base = store.create(_source())
    fingerprint = store.path.read_bytes()
    failed = prepare_allocations(
        store,
        base,
        (AllocationRequest("unlabelled"),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert not failed.report.verified and any(
        issue.code == "allocation.label_missing" for issue in failed.report.issues
    )
    assert store.commit(failed, accept=True).revision is None
    assert store.path.read_bytes() == fingerprint
    unowned = prepare_allocations(
        store,
        base,
        (AllocationRequest("x"),),
        owner="alice",
        prefix="L",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert not unowned.report.verified
    exhausted = prepare_allocations(
        store,
        base,
        tuple(AllocationRequest(str(index), label=Literal("x")) for index in range(6)),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert not exhausted.report.verified and exhausted.draft is None
    overlap = replace(
        _policy(), ranges=(*_policy().ranges, CuratorRange("bob", "LOC", 8, 20, 3))
    )
    bad = AllocationStore(tmp_path / "overlap.sqlite", overlap)
    with pytest.raises(InputError, match="overlap"):
        bad.create(_source())
    assert not bad.path.exists()
    first = prepare_allocations(
        store,
        base,
        (AllocationRequest("one", label=Literal("One")),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    second = prepare_allocations(
        store,
        base,
        (AllocationRequest("two", label=Literal("Two")),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert first.draft is not None and second.draft is not None
    assert reconcile_allocations((first.draft, second.draft)).outcome == "disagreement"
    assert (
        not any(q.predicate == OWL.sameAs for q in first.candidate.patch.additions)
        if first.candidate and first.candidate.patch
        else False
    )
    loaded = read_allocation_draft(allocation_draft_data(first.draft))
    replay = prepare_allocations(
        store,
        base,
        (loaded.assignments[0].request,),
        owner="alice",
        prefix="LOC",
        proposed=loaded,
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert replay.report.verified and store.commit(replay, accept=True).report.verified
    assert store.commit(second, accept=True).revision is None
    assert store.snapshot().revision == 1


def test_parallel_writers_and_cancellation_rollback(tmp_path: Path) -> None:
    """Two actual connections cannot win the same revision; cancellation rolls back."""
    policy = _policy()
    store = AllocationStore(tmp_path / "parallel.sqlite", policy)
    base = store.create(_source())
    proposals = [
        prepare_allocations(
            store,
            base,
            (AllocationRequest(key, label=Literal(key)),),
            owner="alice",
            prefix="LOC",
            after_checks=(MigrationCheck("labels", _labels),),
        )
        for key in ("left", "right")
    ]
    barrier = threading.Barrier(2)

    def commit(index: int) -> int | None:
        """Use separate SQLite owners and synchronize actual commit races."""
        barrier.wait(timeout=10)
        return (
            AllocationStore(store.path, policy)
            .commit(proposals[index], accept=True)
            .revision
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(commit, (0, 1)))
    assert sorted(outcomes, key=lambda value: -1 if value is None else value) == [
        None,
        1,
    ]
    pending = prepare_allocations(
        store,
        store.snapshot(),
        (AllocationRequest("cancelled", label=Literal("Cancel")),),
        owner="bob",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    before = store.path.read_bytes()
    event = threading.Event()

    # A held real writer lock makes the second public call wait; cancellation
    # interrupts that bounded wait without spending or reserving another ID.
    connection = sqlite3.connect(store.path, isolation_level=None)
    connection.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.05, event.set)
    timer.start()
    try:
        rejected = store.commit(pending, accept=True, cancel=event)
    finally:
        connection.execute("ROLLBACK")
        connection.close()
        timer.join()
    assert rejected.revision is None and any(
        issue.kind == "exhausted" for issue in rejected.report.issues
    )
    assert store.path.read_bytes() == before and store.snapshot().revision == 1

    class ObservedStore(AllocationStore):
        """Cancel after a real SQLite UPDATE through its native trace observer."""

        @contextmanager
        def _connection(
            self, operation: Operation, *, create: bool = False
        ) -> Iterator[sqlite3.Connection]:
            """Delegate all storage execution and observe its actual write."""
            with super()._connection(operation, create=create) as actual:

                def trace(statement: str) -> None:
                    """Set cancellation when the native update actually begins."""
                    if statement.startswith("UPDATE allocation"):
                        event.set()

                actual.set_trace_callback(trace)
                yield actual

    event.clear()
    rolled_back = ObservedStore(store.path, policy).commit(
        pending, accept=True, cancel=event
    )
    assert event.is_set() and rolled_back.revision is None
    assert store.path.read_bytes() == before and store.snapshot().revision == 1
    assert store.commit(pending, accept=True).revision == 2


def test_budget_blob_preflight_and_pinned_dataset(tmp_path: Path) -> None:
    """Small readers stop before BLOB materialization and selected graphs stay exact."""
    dataset = Dataset()
    dataset.graph(E.selected).add((E["001"], RDF.type, OWL.Class))
    dataset.graph(E.selected).add((E["001"], RDFS.label, Literal("Existing")))
    dataset.graph(E.other).add((E["005"], RDF.type, OWL.Class))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(E.selected)))
    store = AllocationStore(tmp_path / "pinned.sqlite", _policy())
    base = store.create(source)
    proposal = prepare_allocations(
        store,
        base,
        (AllocationRequest("selected", "concept", Literal("Selected")),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert proposal.report.verified and proposal.draft is not None
    assert proposal.draft.assignments[0].iri == E["004"]
    assert store.commit(proposal, accept=True).report.verified
    assert store.snapshot().source.graph_name == str(E.selected)
    with pytest.raises(LimitExceeded):
        store.snapshot(limits=Limits(max_bytes=64))
    assert store.snapshot().revision == 1
    budget = Budget(Limits(max_states=20))
    cut = prepare_allocations(
        store,
        store.snapshot(),
        (AllocationRequest("cut", label=Literal("Cut")),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
        budget=budget,
    )
    assert (
        any(issue.kind == "exhausted" for issue in cut.report.issues)
        and cut.draft is None
    )
    assert store.snapshot().revision == 1


def test_literal_datatype_references_also_block_fresh_ids(tmp_path: Path) -> None:
    """Existing and newly added datatype IRIs remain named unavailable resources."""
    policy = replace(_policy(), reserved=(), retired=())
    source = prepare_rdf(
        Graph().add((E.subject, E.value, literal_term("x", datatype=str(E["001"]))))
    )
    store = AllocationStore(tmp_path / "datatype.sqlite", policy)
    base = store.create(source)
    first = prepare_allocations(
        store,
        base,
        (AllocationRequest("first", label=literal_term("x", datatype=str(E["003"]))),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert first.report.verified and first.draft is not None
    assert first.draft.assignments[0].iri == E["002"]
    assert store.commit(first, accept=True).report.verified
    reopened = AllocationStore(store.path, policy)
    second = prepare_allocations(
        reopened,
        reopened.snapshot(),
        (AllocationRequest("second", label=Literal("Second")),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert second.report.verified and second.draft is not None
    assert second.draft.assignments[0].iri == E["004"]
    assert reopened.commit(second, accept=True).report.verified

    # Request metadata is already a named reference before choosing an ID.
    fresh = AllocationStore(tmp_path / "request-datatype.sqlite", policy)
    proposed = prepare_allocations(
        fresh,
        fresh.create(prepare_rdf(Graph())),
        (AllocationRequest("fresh", label=literal_term("x", datatype=str(E["001"]))),),
        owner="alice",
        prefix="LOC",
        after_checks=(MigrationCheck("labels", _labels),),
    )
    assert proposed.report.verified and proposed.draft is not None
    assert proposed.draft.assignments[0].iri == E["002"]
    assert fresh.commit(proposed, accept=True).report.verified
