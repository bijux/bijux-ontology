"""Vocabulary resources preserve original graph ownership through scoped loading."""

import pytest
from rdflib import BNode, Dataset, Graph, Namespace
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("urn:source-ownership:")


def test_union_retains_contexts_shared_blanks_and_owned_source_snapshot() -> None:
    dataset = Dataset()
    shared = BNode("shared")
    dataset.default_graph.add((EX.root, EX.link, shared))
    dataset.graph(EX.named).add((shared, EX.link, EX.target))
    vocabulary = load_vocabulary(RdfSource(dataset, union=True))
    dataset.graph(EX.named).add((EX.poison, EX.link, EX.target))
    contexts = tuple(vocabulary._fact_sources(("local",), Budget()))
    assert {(kind, name) for _, kind, name, _ in contexts} == {
        ("default", None),
        ("named", EX.named),
    }
    default = next(graph for _, kind, _, graph in contexts if kind == "default")
    named = next(graph for _, kind, _, graph in contexts if kind == "named")
    owned = next(default.objects(EX.root, EX.link))
    assert (owned, EX.link, EX.target) in named
    assert not any(named.triples((EX.poison, None, None)))
    with pytest.raises(InputError, match="mutation"):
        named.add((EX.poison, EX.link, EX.target))
    # A plain Graph's identifier does not establish dataset graph ownership.
    plain = load_vocabulary(
        Graph(identifier=EX.named).add((EX.root, EX.link, EX.target))
    )
    assert [
        (kind, name) for _, kind, name, _ in plain._fact_sources(("local",), Budget())
    ] == [("graph", None)]
    plain_union = load_vocabulary(
        RdfSource(Graph().add((EX.root, EX.link, EX.target)), union=True)
    )
    assert [
        (kind, name)
        for _, kind, name, _ in plain_union._fact_sources(("local",), Budget())
    ] == [("graph", None)]


def test_required_import_and_prepared_lookup_keep_their_pinned_contexts() -> None:
    imported = Dataset()
    imported.graph(EX.authoritative).add((EX.imported, EX.link, EX.target))
    imported.graph(EX.poison).add((EX.poison, EX.link, EX.target))
    lookup = prepare_rdf(RdfSource(imported, graph_name=str(EX.authoritative)))
    vocabulary = load_vocabulary(
        Graph().add((EX.root, OWL.imports, EX.document)),
        imports={
            str(EX.document): RdfSource(imported, graph_name=str(EX.authoritative))
        },
        lookup=(lookup,),
    )
    contexts = tuple(vocabulary._fact_sources(("imports", "lookup"), Budget()))
    assert {(scope, kind, name) for scope, kind, name, _ in contexts} == {
        ("import[0]", "named", EX.authoritative),
        ("lookup[0]", "named", EX.authoritative),
    }
    assert all(
        not any(graph.triples((EX.poison, None, None))) for *_, graph in contexts
    )
    with pytest.raises(LimitExceeded, match="max_resources"):
        tuple(
            vocabulary._fact_sources(
                ("imports", "lookup"), Budget(Limits(max_resources=1))
            )
        )


def test_owned_blank_graph_selection_survives_vocabulary_preparation() -> None:
    dataset = Dataset()
    dataset.graph(BNode("graph")).add((EX.root, EX.link, EX.target))
    prepared = prepare_rdf(dataset)
    name = next(prepared._graph_names())
    vocabulary = load_vocabulary(prepared, graph_name=name)
    contexts = tuple(vocabulary._fact_sources(("local",), Budget()))
    assert len(contexts) == 1
    _, kind, actual, graph = contexts[0]
    assert kind == "named" and actual == name
    assert (EX.root, EX.link, EX.target) in graph
    with pytest.raises(LimitExceeded, match="max_bytes"):
        load_vocabulary(prepared, graph_name=name, limits=Limits(max_bytes=1))
    with pytest.raises(InputError, match="format and base"):
        load_vocabulary(prepared, graph_name=name, base=str(EX.base))


def test_required_aliases_agree_on_selected_graph_ownership_and_blank_sharing() -> None:
    root = (
        Graph()
        .add((EX.root, OWL.imports, EX.first))
        .add((EX.root, OWL.imports, EX.second))
    )
    left, right = Dataset(), Dataset()
    left.graph(EX.authoritative).add((EX.root, EX.link, EX.target))
    right.graph(EX.other).add((EX.root, EX.link, EX.target))
    first = RdfSource(left, union=True, effective_url=str(EX.second))
    with pytest.raises(ResourceError, match="alias"):
        load_vocabulary(
            root,
            imports={
                str(EX.first): first,
                str(EX.second): RdfSource(right, union=True),
            },
        )
    # Independent blank labels may differ; graph ownership and joint sharing must agree.
    for data, label in ((left, "first"), (right, "second")):
        for context in data.graphs():
            context.remove((None, None, None))
        node = BNode(label)
        data.graph(EX.authoritative).add((EX.root, EX.link, node))
        data.graph(EX.other).add((node, EX.link, EX.target))
    agreed = load_vocabulary(
        root,
        imports={str(EX.first): first, str(EX.second): RdfSource(right, union=True)},
    )
    assert len(agreed.imports) == 1
    right.graph(EX.other).remove((None, None, None))
    right.graph(EX.other).add((BNode("separate"), EX.link, EX.target))
    with pytest.raises(ResourceError, match="alias"):
        load_vocabulary(
            root,
            imports={
                str(EX.first): first,
                str(EX.second): RdfSource(right, union=True),
            },
        )
