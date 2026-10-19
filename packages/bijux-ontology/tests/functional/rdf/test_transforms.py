"""Copied syntactic candidates preserve RDF identity and expose bounded effects."""

import pytest
from rdflib import BNode, Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.transforms import (
    extract_neighbourhood,
    merge_sources,
    recheck_transformation,
    remap_iris,
)
from bijux_ontology.reports import Issue, Report

EX = "https://example.org/"
A, B, P, G = (URIRef(EX + name) for name in ("A", "B", "p", "graph"))


def test_extraction_keeps_attached_list_and_omits_unselected_edges() -> None:
    dataset = Dataset()
    head = BNode("head")
    dataset.graph(G).add((A, P, head))
    dataset.graph(G).add((head, RDF.first, Literal("First")))
    dataset.graph(G).add((head, RDF.rest, RDF.nil))
    dataset.graph(G).add((A, URIRef(EX + "unselected"), Literal("Unselected")))
    dataset.default_graph.add((B, P, head))
    original = prepare_source(dataset)
    candidate = extract_neighbourhood(original, (str(A),), predicates=(str(P),))
    assert candidate.source is not None
    exported = candidate.source.dataset()
    assert len(exported.graph(G)) == 3
    assert len(exported.default_graph) == 0
    assert len(original.dataset()) == 5
    truncated = extract_neighbourhood(
        original, (str(A),), predicates=(str(P),), include_blank_nodes=False
    )
    assert truncated.source is not None
    assert len(truncated.source.dataset()) == 1
    bounded = extract_neighbourhood(
        original, (str(A),), predicates=(str(P),), limits=Limits(max_work=1)
    )
    assert bounded.source is None
    assert bounded.report.outcome == "exhausted"
    with pytest.raises(InputError):
        extract_neighbourhood(original, (), predicates=(str(P),))


def test_remapping_preserves_positions_lexical_text_and_detects_collisions() -> None:
    dataset = Dataset()
    dataset.graph(G).add((A, P, Literal("01", datatype=XSD.integer, normalize=False)))
    dataset.graph(URIRef(EX + "empty"))
    original = prepare_source(dataset)
    mapping = {
        str(A): str(B),
        str(P): EX + "new-p",
        str(G): EX + "new-graph",
        str(XSD.integer): EX + "number",
    }
    changed = remap_iris(original, mapping)
    assert changed.source is not None
    with pytest.raises(InputError):
        remap_iris(original, {str(A): str(B), "not absolute": str(P)})
    graph = changed.source.graph(EX + "new-graph")
    assert (
        B,
        URIRef(EX + "new-p"),
        Literal("01", datatype=URIRef(EX + "number")),
    ) in graph
    assert len(changed.source.graph(EX + "empty")) == 0
    assert list(original.graph(G).subjects()) == [A]
    collision = remap_iris(original, {str(A): str(P)})
    assert collision.source is None
    assert "rdf.remap_collision" in {issue.code for issue in collision.report.issues}
    # A swap has distinct final identities, despite targets also appearing as inputs.
    swapped = remap_iris(original, {str(A): str(P), str(P): str(A)})
    assert swapped.source is not None
    assert (P, A, None) in swapped.source.graph(G)
    datatype_collision = remap_iris(original, {str(P): str(XSD.integer)})
    assert datatype_collision.source is None
    plain = prepare_source(Graph().add((A, P, Literal("Word"))))
    plain_remapped = remap_iris(plain, {str(XSD.string): EX + "text"})
    assert plain_remapped.source is not None
    value = next(plain_remapped.source.graph().objects())
    assert isinstance(value, Literal) and value.datatype == URIRef(EX + "text")
    assert str(value) == "Word"
    language = prepare_source(Graph().add((A, P, Literal("Colour", lang="en-GB"))))
    invalid_language = remap_iris(language, {str(RDF.langString): EX + "text"})
    assert invalid_language.source is None
    assert "rdf.remap_literal_loss" in {
        issue.code for issue in invalid_language.report.issues
    }


def test_merging_separates_blanks_and_retains_explicit_dataset_semantics() -> None:
    snapshots: list[PreparedSource] = []
    for index in range(2):
        dataset = Dataset()
        node = BNode("same")
        dataset.graph(G).add((node, P, Literal(str(index))))
        dataset.default_graph.add((A, OWL.imports, URIRef(EX + "import")))
        dataset.graph(URIRef(EX + "empty"))
        dataset.bind("ex", URIRef(EX + f"namespace{index}/"))
        snapshots.append(prepare_source(dataset))
    merged = merge_sources(snapshots)
    assert merged.source is not None and merged.report.verified
    assert len(set(merged.source.graph(G).subjects())) == 2
    assert (A, OWL.imports, URIRef(EX + "import")) in merged.source.graph("@default")
    assert len(merged.source.graph(EX + "empty")) == 0
    prefixes = {str(iri) for _, iri in merged.source.dataset().namespaces()}
    assert {EX + "namespace0/", EX + "namespace1/"} <= prefixes
    union = merge_sources(snapshots, union=True)
    assert union.source is not None
    assert not union.source.is_dataset
    assert len(union.source.graph()) == 3
    assert all(len(source.graph(G)) == 1 for source in snapshots)


def test_transformations_respect_declared_graph_views_and_retain_scope_failures() -> (
    None
):
    dataset = Dataset()
    dataset.default_graph.add((A, P, Literal("default")))
    dataset.graph(G).add((A, P, Literal("selected")))
    dataset.graph(URIRef(EX + "hidden")).add((B, P, Literal("hidden")))
    pinned = prepare_source(RdfSource(dataset, graph_name=str(G)))
    merged = merge_sources((pinned,))
    assert merged.source is not None and merged.report.verified
    assert set(merged.source.graph(G)) == {(A, P, Literal("selected"))}
    assert not merged.source.graph("@default")
    assert set(merged.source._graph_names()) == {G}
    extracted = extract_neighbourhood(pinned, (str(A),), predicates=(str(P),))
    assert extracted.source is not None and extracted.report.verified
    assert extracted.source.graph_name == str(G)
    assert set(extracted.source.graph()) == {(A, P, Literal("selected"))}
    assert set(extracted.source._graph_names()) == {G}
    renamed_graph = URIRef(EX + "renamed")
    # B appears only outside the authorized view and cannot create a collision.
    rewritten = remap_iris(pinned, {str(A): str(B), str(G): str(renamed_graph)})
    assert rewritten.source is not None and rewritten.report.verified
    assert rewritten.source.graph_name == str(renamed_graph)
    assert set(rewritten.source.graph()) == {(B, P, Literal("selected"))}
    assert set(rewritten.source._graph_names()) == {renamed_graph}
    observed = []

    def selected_only(source: PreparedSource) -> Report:
        """A trusted check must receive only its source's authorized snapshot."""
        observed.append(source.graph_name)
        assert len(source.dataset()) == 1
        assert set(source.graph().objects()) == {Literal("selected")}
        return Report("selected", 1)

    rechecked = recheck_transformation(
        pinned, rewritten.source, {"selected": selected_only}
    )
    assert rechecked.verified and observed == [str(G), str(renamed_graph)]
    empty = extract_neighbourhood(pinned, (str(B),), predicates=(str(P),))
    assert empty.source is not None and not empty.source.graph()
    assert empty.report.outcome == "not_applicable" and empty.report.exit_code() == 0
    assert empty.source.graph_name == str(G) and set(empty.source._graph_names()) == {G}
    default = merge_sources(
        (prepare_source(RdfSource(dataset, graph_name="@default")),)
    )
    assert default.source is not None and default.report.verified
    assert set(default.source.graph("@default")) == {(A, P, Literal("default"))}
    assert not tuple(default.source._graph_names())
    declared_union = merge_sources((prepare_source(RdfSource(dataset, union=True)),))
    assert declared_union.source is not None and declared_union.report.verified
    assert (
        not declared_union.source.is_dataset and len(declared_union.source.graph()) == 3
    )
    union_source = prepare_source(RdfSource(dataset, union=True))
    union_rewritten = remap_iris(union_source, {str(P): EX + "replacement"})
    assert union_rewritten.source is not None and union_rewritten.report.verified
    assert union_rewritten.source.union and len(union_rewritten.source.graph()) == 3
    assert not tuple(union_rewritten.source._graph_names())
    unknown = merge_sources(
        (
            PreparedSource(
                dataset,
                is_dataset=True,
                document_iri=None,
                effective_url=None,
                literal_fidelity="source_terms",
                graph_fidelity="nonempty-only",
            ),
        )
    )
    assert unknown.report.outcome == "unverified"
    stopped = merge_sources((pinned,), limits=Limits(max_triples=1, max_work=1))
    assert stopped.source is None and stopped.report.outcome == "exhausted"


def test_candidate_rechecks_preserve_outcomes_and_callback_ownership() -> None:
    graph = Graph().add((A, RDF.type, OWL.Class))
    original = prepare_source(graph)
    transformed = remap_iris(original, {str(OWL.Class): EX + "Other"})
    assert transformed.source is not None

    def declaration(source: PreparedSource) -> Report:
        """The caller chooses the supported assertion to check on both snapshots."""
        supplied = source.graph()
        present = (A, RDF.type, OWL.Class) in supplied
        supplied.remove((None, None, None))
        return Report(
            "declarations",
            1,
            () if present else (Issue("example.missing", "Class declaration missing"),),
        )

    report = recheck_transformation(
        original, transformed.source, {"class": declaration}
    )
    assert [child.scope for child in report.children] == ["before.class", "after.class"]
    assert [child.exit_code() for child in report.children] == [0, 1]
    assert len(original.graph()) == 1
    assert len(transformed.source.graph()) == 1

    def failed(source: PreparedSource) -> Report:
        """Trusted callback failures retain their original identity."""
        raise RuntimeError("Deliberate callback failure")

    with pytest.raises(RuntimeError, match="Deliberate"):
        recheck_transformation(original, transformed.source, {"failure": failed})
