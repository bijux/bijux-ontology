"""RDF identity preserves blank-node structure, lexical values and graph scope."""

from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.collection import Collection
from rdflib.namespace import XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.comparison import compare_rdf

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
    assert compare_rdf(left, changed).outcome == "disagreement"
    assert not compare_rdf(left, changed).verified
    assert compare_rdf(Graph(), Graph()).verified


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
    left = Graph().add((EX.A, EX.p, EX.B))
    report = compare_rdf(left, left, timeout_seconds=0.000001)
    assert report.outcome == "exhausted" and not report.complete
    assert report.issues[-1].kind == "exhausted"


def test_incomplete_empty_graph_scope_needs_an_explicit_comparison_exclusion() -> None:
    parsed = prepare_source("<urn:g> {}", format="trig")
    actual = Dataset()
    actual.graph(Namespace("urn:").g)
    unresolved = compare_rdf(parsed, actual)
    assert unresolved.outcome == "unverified" and not unresolved.complete
    narrowed = compare_rdf(parsed, actual, ignore_empty_graphs=True)
    assert narrowed.verified and narrowed.exclusions == ("empty-named-graphs",)
    assert narrowed.regime == "rdf-nonempty-graph-structure"
