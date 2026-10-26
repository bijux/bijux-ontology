"""RDF identity preserves blank-node structure, lexical values and graph scope."""

import threading
import time

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("https://example.org/")


def test_blank_node_renaming_is_ignored_but_changed_list_order_is_visible() -> None:
    left = Graph()
    head = BNode("left")
    Collection(left, head, [EX.A, EX.B])
    left.add((EX.record, EX.values, head))
    right = Graph()
    other = BNode("right")
    Collection(right, other, [EX.A, EX.B])
    right.add((EX.record, EX.values, other))
    assert compare_rdf(left, right).verified
    changed = Graph()
    Collection(changed, other, [EX.B, EX.A])
    changed.add((EX.record, EX.values, other))
    different = compare_rdf(left, changed)
    assert different.outcome == "disagreement" and not different.verified
    assert compare_rdf(Graph(), Graph()).verified
    with pytest.raises(TypeError, match="_owned_worker"):
        compare_rdf(left, right, _owned_worker=True)  # type: ignore[call-arg]


def test_dataset_names_empty_graphs_and_cross_graph_blank_identity_survive() -> None:
    left = Dataset()
    left.default_graph.add((EX.A, EX.value, Literal("one")))
    shared = BNode("shared")
    left.graph(EX.graph).add((EX.A, EX.child, shared))
    left.graph(EX.other).add((shared, EX.name, Literal("child")))
    left.graph(EX.empty)
    right = Dataset()
    right.default_graph.add((EX.A, EX.value, Literal("one")))
    renamed = BNode("renamed")
    right.graph(EX.graph).add((EX.A, EX.child, renamed))
    right.graph(EX.other).add((renamed, EX.name, Literal("child")))
    right.graph(EX.empty)
    assert compare_rdf(left, right).verified
    right.graph(EX.other).remove((renamed, None, None))
    right.graph(EX.other).add((BNode("different"), EX.name, Literal("child")))
    assert compare_rdf(left, right).outcome == "disagreement"
    right.remove_graph(EX.empty)
    assert compare_rdf(left, right).outcome == "disagreement"
    moved = Dataset()
    moved.graph(EX.graph).add((EX.A, EX.value, Literal("one")))
    default = Dataset()
    default.default_graph.add((EX.A, EX.value, Literal("one")))
    assert compare_rdf(default, moved).outcome == "disagreement"


def test_literal_identity_and_bounded_search_cannot_be_weakened_to_value_equality() -> (
    None
):
    left = Graph().add(
        (EX.A, EX.value, Literal("01", datatype=XSD.integer, normalize=False))
    )
    right = Graph().add(
        (EX.A, EX.value, Literal("1", datatype=XSD.integer, normalize=False))
    )
    assert compare_rdf(left, right).outcome == "disagreement"
    upper = Graph().add((EX.A, EX.label, Literal("Name", lang="EN")))
    lower = Graph().add((EX.A, EX.label, Literal("Name", lang="en")))
    assert compare_rdf(upper, lower).verified
    simple = Graph().add((EX.A, EX.value, Literal("text")))
    explicit = Graph().add((EX.A, EX.value, Literal("text", datatype=XSD.string)))
    assert compare_rdf(simple, explicit).verified
    blank = Graph().add((BNode("a"), EX.p, BNode("b")))
    report = compare_rdf(blank, blank, limits=Limits(max_work=1))
    assert report.outcome == "exhausted" and not report.complete
    assert not report.verified


def test_record_encoding_preserves_ambiguous_topology_and_term_roles() -> None:
    cycle = Graph()
    pairs = Graph()
    nodes = [BNode(str(index)) for index in range(4)]
    for index, node in enumerate(nodes):
        cycle.add((node, EX.p, nodes[(index + 1) % 4]))
        pairs.add((node, EX.p, nodes[index ^ 1]))
    assert compare_rdf(cycle, pairs).outcome == "disagreement"
    named = Graph().add((EX.A, EX.p, EX.B))
    lexical = Graph().add((EX.A, EX.p, Literal(str(EX.B))))
    assert compare_rdf(named, lexical).outcome == "disagreement"


def test_comparison_worker_deadline_is_an_incomplete_result() -> None:
    left = Graph().add((EX.A, EX.p, BNode()))
    report = compare_rdf(left, left, timeout_seconds=0.000001)
    assert report.outcome == "exhausted" and not report.complete
    assert report.issues[-1].kind == "exhausted"


def test_ground_datasets_compare_without_starting_a_correspondence_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_worker(*args: object, **kwargs: object) -> None:
        raise AssertionError("Ground comparison must not start a worker")

    monkeypatch.setattr(
        "bijux_ontology.rdf.comparison.execute_worker", unexpected_worker
    )
    left = Dataset()
    right = Dataset()
    for index in range(1000):
        triple = (EX[f"s{index}"], EX.p, Literal(index))
        left.default_graph.add(triple)
        right.default_graph.add(triple)
    assert compare_rdf(left, right).verified
    right.default_graph.set((EX.s999, EX.p, Literal(-1)))
    assert compare_rdf(left, right).outcome == "disagreement"
    right.default_graph.set((EX.s999, EX.p, Literal(999)))
    right.graph(EX.empty)
    assert compare_rdf(left, right).outcome == "disagreement"
    left.graph(EX.empty)
    assert compare_rdf(left, right).verified


def test_blank_graph_names_retain_correspondence_and_distinct_graph_scope() -> None:
    left = Dataset()
    right = Dataset()
    left.graph(BNode("left")).add((EX.A, EX.p, EX.B))
    right.graph(BNode("right")).add((EX.A, EX.p, EX.B))
    assert compare_rdf(left, right).verified
    right.graph(BNode("other"))
    assert compare_rdf(left, right).outcome == "disagreement"


def test_incomplete_empty_graph_scope_needs_an_explicit_comparison_exclusion() -> None:
    parsed = PreparedSource(
        Dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source_terms",
        graph_fidelity="nonempty-only",
    )
    actual = Dataset()
    actual.graph(Namespace("urn:").g)
    unresolved = compare_rdf(parsed, actual)
    assert unresolved.outcome == "unverified" and not unresolved.complete
    narrowed = compare_rdf(parsed, actual, ignore_empty_graphs=True)
    assert narrowed.verified and narrowed.exclusions == ("empty-named-graphs",)
    assert narrowed.regime == "rdf-nonempty-graph-structure"


def test_prepared_graph_selection_excludes_hidden_data_and_union_is_explicit() -> None:
    left = Dataset()
    left.graph(EX.selected).add((EX.A, EX.p, EX.B))
    left.graph(EX.hidden).add((EX.A, EX.p, EX.C))
    right = Dataset()
    right.graph(EX.selected).add((EX.A, EX.p, EX.B))
    right.graph(EX.hidden).add((EX.A, EX.p, EX.D))
    selected = prepare_rdf(RdfSource(left, graph_name=str(EX.selected)))
    same = prepare_rdf(RdfSource(right, graph_name=str(EX.selected)))
    different = prepare_rdf(RdfSource(left, graph_name=str(EX.hidden)))
    assert compare_rdf(selected, same).verified
    assert compare_rdf(selected, different).outcome == "disagreement"
    assert compare_rdf(selected, selected).verified
    assert compare_rdf(selected, left).outcome == "disagreement"
    projected = prepare_rdf(RdfSource(left, union=True))
    flat = Graph().add((EX.A, EX.p, EX.B)).add((EX.A, EX.p, EX.C))
    assert compare_rdf(projected, flat).verified
    default = prepare_rdf(RdfSource(left, graph_name="@default"))
    assert compare_rdf(default, Graph()).verified
    assert compare_rdf(projected, default).outcome == "disagreement"


def test_intrinsic_literal_aliases_have_set_semantics_without_lexical_coercion() -> (
    None
):
    for subject in (EX.A, BNode()):
        aliases = Graph().add((subject, EX.p, Literal("same")))
        aliases.add((subject, EX.p, Literal("same", datatype=XSD.string)))
        single = Graph().add((subject, EX.p, Literal("same", datatype=XSD.string)))
        assert compare_rdf(aliases, single).verified
        aliases.add(
            (subject, EX.p, Literal("01", datatype=XSD.integer, normalize=False))
        )
        single.add((subject, EX.p, Literal("1", datatype=XSD.integer, normalize=False)))
        assert compare_rdf(aliases, single).outcome == "disagreement"


def test_cancelled_comparison_is_incomplete_and_a_fresh_call_recovers() -> None:
    """Cancellation cannot turn empty, ground or blank identity into agreement."""
    cancel = threading.Event()
    cancel.set()
    for graph in (
        Graph(),
        Graph().add((EX.A, EX.p, EX.B)),
        Graph().add((EX.A, EX.p, BNode())),
    ):
        report = compare_rdf(graph, graph, cancel=cancel)
        assert report.outcome == "exhausted" and not report.complete
        assert report.checked == 0
    cancel.clear()
    assert compare_rdf(graph, graph, cancel=cancel).verified


def test_active_blank_comparison_cancels_before_its_worker_deadline() -> None:
    """A live cancellation interrupts the maintained correspondence process."""
    graph = Graph()
    nodes = [BNode() for _ in range(100)]
    for index, node in enumerate(nodes):
        graph.add((node, EX.p, nodes[(index + 1) % len(nodes)]))
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()
    start = time.monotonic()
    try:
        report = compare_rdf(graph, graph, timeout_seconds=10, cancel=cancel)
    finally:
        timer.cancel()
        timer.join()
    assert report.outcome == "exhausted" and not report.complete
    assert time.monotonic() - start < 2


def test_completed_native_reply_after_parent_deadline_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bijux_ontology.rdf.comparison as owner
    from bijux_ontology.execution.budgets import Operation
    from bijux_ontology.execution.limits import Budget

    parent = Operation(None, Budget(), 3, None)
    graph = Graph().add((EX.root, EX.p, BNode()))
    original = owner._isomorphic
    replies = []

    def delayed(
        payload: bytes,
        timeout_seconds: float,
        cancel: threading.Event | None,
        *,
        _owned_worker: bool = False,
    ) -> bool:
        answer = original(payload, timeout_seconds, cancel, _owned_worker=_owned_worker)
        replies.append(answer)
        # Delay only the arrival of a genuine maintained comparator reply.
        time.sleep(max(0, parent.deadline - time.monotonic()) + 0.01)
        return answer

    monkeypatch.setattr(owner, "_isomorphic", delayed)
    report = compare_rdf(graph, graph, budget=parent, timeout_seconds=10)
    assert replies == [True]
    assert report.outcome == "exhausted" and not report.verified
    assert report.checked == 0 and report.requested == 1


@pytest.mark.parametrize("operation", ("comparison", "canonical"))
def test_parent_cancellation_owns_actual_rdf_worker_and_conflicts_are_rejected(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    import subprocess
    from typing import Any

    import bijux_ontology.execution.processes.transport as transport
    import bijux_ontology.rdf.canonical.export as canonical
    import bijux_ontology.rdf.comparison as comparison
    from bijux_ontology.diagnostics.errors import InputError
    from bijux_ontology.execution.budgets import Operation
    from bijux_ontology.execution.limits import Budget
    from bijux_ontology.execution.processes.ownership import spawn_owned_process
    from bijux_ontology.execution.workers import execute_worker

    graph = Graph().add((EX.root, EX.p, BNode()))
    cancel = threading.Event()
    parent = Operation(None, Budget(), 3, cancel)
    owner = comparison if operation == "comparison" else canonical
    original_worker, original_spawn = execute_worker, spawn_owned_process
    calls: list[dict[str, Any]] = []
    children: list[subprocess.Popen[bytes]] = []

    def spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = original_spawn(*args, **kwargs)
        children.append(child)
        cancel.set()
        return child

    def execute(*args: Any, **kwargs: Any) -> bytes:
        calls.append(kwargs)
        return original_worker(*args, **kwargs)

    def check(*, inherited: bool, event: threading.Event | None = None) -> Report:
        budget = parent if inherited else None
        if operation == "comparison":
            return comparison.compare_rdf(
                graph, graph, budget=budget, cancel=event, timeout_seconds=10
            )
        result = canonical.canonicalize_rdf(
            graph, budget=budget, cancel=event, timeout_seconds=10
        )
        assert result.content is None or result.report.verified
        return result.report

    monkeypatch.setattr(transport, "spawn_owned_process", spawn)
    monkeypatch.setattr(owner, "execute_worker", execute)
    report = check(inherited=True)
    assert report.outcome == "exhausted" and not report.complete
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].returncode != 0
    assert calls[0]["cancel"] is cancel and 0 < calls[0]["timeout"] <= 3
    with pytest.raises(InputError, match="same Event"):
        check(inherited=True, event=threading.Event())
    assert len(children) == 1
    assert check(inherited=False).verified
    assert all(child.poll() is not None for child in children)
