"""Owned readers share immutable facts without changing source scope or reports."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SKOS

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://reader.example/")


def test_owned_read_boundary_rejects_graph_store_and_namespace_writes() -> None:
    original = Graph().add((EX.Item, RDF.type, OWL.Class))
    vocabulary = Vocabulary(original)
    reader = vocabulary._read().graph
    original.remove((None, None, None))
    for operation in (
        lambda: reader.add((EX.Other, RDF.type, OWL.Class)),
        lambda: reader.remove((None, None, None)),
        lambda: reader.store.add((EX.Other, RDF.type, OWL.Class), reader),
        lambda: reader.store.remove((None, None, None), reader),
        lambda: reader.bind("other", EX),
        lambda: reader.parse(data="<urn:a> <urn:b> <urn:c> .", format="turtle"),
    ):
        with pytest.raises(InputError, match="mutation"):
            operation()
    assert all(
        not tuple(contexts) for _, contexts in reader.store.triples((None, None, None))
    )
    vocabulary.local.remove((None, None, None))
    vocabulary.graph().remove((None, None, None))
    inspect_vocabulary(vocabulary).graph().remove((None, None, None))
    assert check_vocabulary(vocabulary, [TermDeclaration(str(EX.Item))]).verified
    assert len(reader) == 1


def test_independent_input_blank_scopes_and_role_selections_stay_separate() -> None:
    blank = BNode("same-input-label")
    local = Graph().add((EX.local, EX.link, blank))
    imported = Graph().add((EX.imported, EX.link, blank))
    lookup = Graph().add((EX.lookup, EX.link, blank))
    lookup.add((EX.LookupOnly, RDF.type, OWL.Class))
    vocabulary = Vocabulary(local, [imported], [lookup])
    full = vocabulary.graph()
    assert len(set(full.objects(None, EX.link))) == 3
    assert len(set(inspect_vocabulary(vocabulary).graph().objects(None, EX.link))) == 2
    assert inspect_vocabulary(vocabulary).lookup(str(EX.LookupOnly)).status == "absent"
    assert (
        inspect_vocabulary(vocabulary, scopes=("lookup",))
        .lookup(str(EX.LookupOnly))
        .status
        == "declared"
    )
    vocabulary.imports[0].remove((None, None, None))
    vocabulary.lookup[0].remove((None, None, None))
    for original in (local, imported, lookup):
        original.remove((None, None, None))
    assert len(vocabulary.imports[0]) == 1
    assert len(vocabulary.lookup[0]) == 2
    assert check_vocabulary(vocabulary, [TermDeclaration(str(EX.LookupOnly))]).verified
    assert not check_vocabulary(
        vocabulary, [TermDeclaration(str(EX.LookupOnly))], local_namespaces=(str(EX),)
    ).verified


def test_concurrent_role_readers_and_new_resources_have_independent_answers() -> None:
    original = Graph().add((EX.Concept, RDF.type, SKOS.Concept))
    original.add((EX.Concept, SKOS.prefLabel, Literal("Name", lang="en")))
    lookup = Graph().add((EX.Extra, RDF.type, OWL.Class))
    vocabulary = Vocabulary(original, lookup=[lookup])

    def read(index: int) -> tuple[bool, str, str]:
        """Exercise lazy cache publication and defensive exports concurrently."""
        scopes = ("local", "lookup") if index % 2 else ("local",)
        view = inspect_vocabulary(vocabulary, scopes=scopes)
        view.graph().remove((None, None, None))
        return (
            check_skos(view).verified,
            view.lookup(str(EX.Extra)).status,
            view.preferred_label(str(EX.Concept)).preferred[0].value,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        answers = tuple(executor.map(read, range(32)))
    assert answers == tuple(
        (True, "declared" if index % 2 else "absent", "Name") for index in range(32)
    )
    original.add((EX.Concept, SKOS.prefLabel, Literal("Conflicting", lang="en")))
    assert check_skos(vocabulary).verified
    assert not check_skos(Vocabulary(original)).verified


def test_failed_index_preparation_and_smaller_limits_do_not_poison_cached_views() -> (
    None
):
    vocabulary = load_vocabulary(
        "@prefix skos:<http://www.w3.org/2004/02/skos/core#>. "
        '<https://reader.example/Concept> a skos:Concept; skos:prefLabel "Name"@en.',
        format="turtle",
    )
    exhausted = check_skos(vocabulary, limits=Limits(max_work=1))
    assert not exhausted.complete and exhausted.outcome == "exhausted"
    assert check_skos(vocabulary).verified
    assert inspect_vocabulary(vocabulary).terms().report.checked == 1
    with pytest.raises(LimitExceeded, match="max_triples"):
        inspect_vocabulary(vocabulary, limits=Limits(max_triples=1))
    assert check_skos(vocabulary).verified


def test_public_prepared_source_still_owns_dataset_and_preserves_blank_relations() -> (
    None
):
    dataset = Dataset()
    blank = BNode("shared")
    dataset.default_graph.add((EX.root, EX.link, blank))
    dataset.graph(EX.named).add((blank, EX.label, Literal("Name")))
    prepared = PreparedSource(
        dataset,
        is_dataset=True,
        document_iri=None,
        effective_url=None,
        literal_fidelity="supplied_terms",
    )
    dataset.default_graph.remove((None, None, None))
    dataset.graph(EX.named).remove((None, None, None))
    exported = prepared.dataset()
    selected_blank = next(exported.default_graph.objects(EX.root, EX.link))
    assert (selected_blank, EX.label, Literal("Name")) in exported.graph(EX.named)
    exported.graph(EX.named).remove((None, None, None))
    assert len(prepared.graph(str(EX.named))) == 1
    native = prepare_source(prepared.dataset())
    assert len(native.graph("@default")) == 1
    assert len(native.graph(str(EX.named))) == 1
    with pytest.raises(InputError, match="mutation"):
        native._read(str(EX.named)).remove((None, None, None))
