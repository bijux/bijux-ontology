"""Canonical artifacts preserve standard bytes, exact terms and incomplete scope."""

import json
import threading

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.canonical import canonicalize_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

EX = Namespace("https://canonical.example/")


def test_reordered_renamed_dataset_preserves_cross_graph_identity_and_literals() -> (
    None
):
    datasets = []
    for label, reverse in (("first", False), ("other", True)):
        dataset = Dataset()
        shared = BNode(label)
        records = [
            (EX.root, EX.child, shared, None),
            (
                shared,
                EX.value,
                Literal("01", datatype=XSD.integer, normalize=False),
                shared,
            ),
            (shared, EX.label, Literal("Café\n世界", lang="FR"), EX.named),
        ]
        for subject, predicate, value, graph in (
            reversed(records) if reverse else records
        ):
            selected = dataset.default_graph if graph is None else dataset.graph(graph)
            selected.add((subject, predicate, value))
        datasets.append(dataset)
    before = frozenset(datasets[0].quads())
    left, right = (canonicalize_rdf(dataset) for dataset in datasets)
    assert left.report.verified and right.report.verified
    assert left.content == right.content and left.quad_count == 3
    assert left.content is not None and b'"01"^^' in left.content
    assert "世界" in left.content.decode("utf-8") and b"\\n" in left.content
    assert b"@fr" in left.content
    assert frozenset(datasets[0].quads()) == before
    assert left.algorithm == "RDFC-1.0" and left.rdf_version == "1.1"
    for named in datasets[1].graphs():
        for changed_subject in tuple(named.subjects(EX.value, None)):
            named.set(
                (
                    changed_subject,
                    EX.value,
                    Literal("1", datatype=XSD.integer, normalize=False),
                )
            )
    changed = canonicalize_rdf(datasets[1])
    assert changed.report.verified and changed.content != left.content


@pytest.mark.parametrize("algorithm,labels", (("SHA256", (2, 0)), ("SHA384", (0, 2))))
def test_official_diamond_vectors_select_explicit_standard_hash(
    algorithm: str, labels: tuple[int, int]
) -> None:
    # W3C rdf-canon official tests020/075; SHA384 changes canonical blank labels.
    prefix = "http://example.org/vocab#"
    text = (
        f"<{prefix}test> <{prefix}A> _:a .\n"
        f"<{prefix}test> <{prefix}B> _:b .\n"
        f"_:a <{prefix}next> _:c .\n_:b <{prefix}next> _:c .\n"
    )
    expected = (
        f"<{prefix}test> <{prefix}A> _:c14n{labels[0]} .\n"
        f"<{prefix}test> <{prefix}B> _:c14n{labels[1]} .\n"
        f"_:c14n0 <{prefix}next> _:c14n1 .\n"
        f"_:c14n2 <{prefix}next> _:c14n1 .\n"
    ).encode()
    result = canonicalize_rdf(
        prepare_source(text, format="nquads"), hash_algorithm=algorithm
    )
    assert result.report.verified and result.content == expected
    assert result.hash_algorithm == algorithm


def test_empty_graph_inventory_requires_a_visible_explicit_quad_only_scope() -> None:
    empty = Dataset()
    empty.graph(EX.empty)
    result = canonicalize_rdf(empty)
    assert result.content is None and not result.report.complete
    assert any(
        i.code == "rdf.canonicalization_empty_graph_loss" for i in result.report.issues
    )
    excluded = canonicalize_rdf(empty, ignore_empty_graphs=True)
    absent = canonicalize_rdf(Dataset())
    assert excluded.content == absent.content == b""
    assert excluded.report.verified and absent.report.verified
    assert excluded.report.exclusions == ("empty-named-graphs",)
    unknown = PreparedSource(
        Dataset(),
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="source_terms",
        graph_fidelity="nonempty-only",
    )
    uncertain = canonicalize_rdf(unknown)
    assert uncertain.content is None and uncertain.report.outcome == "unverified"
    assert any(
        i.code == "rdf.canonicalization_graph_scope_unverified"
        for i in uncertain.report.issues
    )


def test_pinned_graph_and_literal_fidelity_are_independent_of_dataset_inventory() -> (
    None
):
    dataset = Dataset()
    dataset.graph(EX.selected).add((EX.root, EX.value, Literal("selected")))
    dataset.graph(EX.other).add((EX.root, EX.value, Literal("other")))
    dataset.graph(EX.empty)
    source = prepare_source(RdfSource(dataset, graph_name=str(EX.selected)))
    selected = canonicalize_rdf(source)
    assert selected.report.verified and selected.quad_count == 1
    assert selected.quad_scope == "named" and selected.graph_name == str(EX.selected)
    assert selected.content is not None and b'"selected"' in selected.content
    assert b'"other"' not in selected.content
    normalized = prepare_source(
        '<urn:s> <urn:p> "a  b"^^<http://www.w3.org/2001/XMLSchema#token> .',
        format="nt",
        preserve_literals=False,
    )
    refused = canonicalize_rdf(normalized)
    assert (
        refused.content is None and refused.literal_fidelity == "normalisation_allowed"
    )
    assert refused.report.outcome == "unverified"


def test_shared_limits_cancel_and_difficult_search_never_publish_partial_bytes() -> (
    None
):
    graph = Graph().add((EX.root, EX.p, BNode()))
    shared = Budget(Limits(max_work=2))
    shared.consume("work", 2)
    limited = canonicalize_rdf(graph, budget=shared)
    assert limited.content is None and limited.report.outcome == "exhausted"
    two = Graph().add((EX.root, EX.p, Literal(1))).add((EX.root, EX.p, Literal(2)))
    assert (
        canonicalize_rdf(two, limits=Limits(max_triples=1)).report.outcome
        == "exhausted"
    )
    named = Dataset()
    for name in (EX.one, EX.two):
        named.graph(name).add((EX.root, EX.p, Literal("x" * 60)))
    oversized = canonicalize_rdf(named, limits=Limits(max_bytes=250))
    assert oversized.content is None and oversized.report.outcome == "exhausted"
    cancel = threading.Event()
    cancel.set()
    cancelled = canonicalize_rdf(graph, cancel=cancel)
    assert cancelled.content is None and cancelled.report.outcome == "exhausted"
    poison = Graph()
    nodes = [BNode(str(index)) for index in range(10)]
    for left in nodes:
        for right in nodes:
            if left != right:
                poison.add((left, EX.related, right))
    exhausted = canonicalize_rdf(poison, timeout_seconds=0.5)
    assert exhausted.content is None and exhausted.report.outcome == "exhausted"
    assert canonicalize_rdf(graph).report.verified
    with pytest.raises(InputError):
        canonicalize_rdf(graph, hash_algorithm="URDNA2015")


@pytest.mark.parametrize("body", (None, "garbage .\n", "_:wrong <urn:p> <urn:o> .\n"))
def test_malformed_or_unsupported_worker_output_never_becomes_a_publication(
    body: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = (
        {}
        if body is None
        else {
            "status": "result",
            "algorithm": "RDFC-1.0",
            "hash_algorithm": "SHA256",
            "rdf_version": "1.1",
            "backend": "pyoxigraph/0.5.11",
            "quad_count": 1,
            "content": body,
        }
    )
    monkeypatch.setattr(
        "bijux_ontology.rdf.canonical.export.execute_worker",
        lambda *args, **kwargs: json.dumps(response).encode(),
    )
    invalid = canonicalize_rdf(Graph().add((EX.root, EX.p, Literal("value"))))
    assert invalid.content is None and invalid.report.outcome == "input_error"
