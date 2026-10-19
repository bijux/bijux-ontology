"""OntoLex asserted structure, exact RDF round-trips and explicit semantic limits."""

from threading import Event

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.ontolex import (
    LexicalPolicy,
    Lexicon,
    lookup_written,
    project_labels,
    read_lexicon,
    write_lexicon,
)
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

EX = Namespace("urn:ontolex-test:")
ONTOLEX = Namespace("http://www.w3.org/ns/lemon/ontolex#")
LI = Namespace("http://www.lexinfo.net/ontology/2.0/lexinfo#")


def _consumer() -> tuple[Graph, VocabularyView, tuple[URIRef, ...]]:
    """Produce an ordinary finite RDF lexicon and independently selected ontology."""
    vocabulary = Graph()
    vocabulary.add((EX.Cat, RDF.type, OWL.Class))
    vocabulary.add((EX.symbol, RDF.type, RDF.Property))
    graph = Graph()
    for entry in (EX.cat, EX.second):
        graph.add((entry, RDF.type, ONTOLEX.LexicalEntry))
    graph.add((EX.cat, ONTOLEX.canonicalForm, EX.singular))
    graph.add((EX.cat, ONTOLEX.otherForm, EX.plural))
    graph.add((EX.cat, ONTOLEX.otherForm, EX.regional))
    graph.add((EX.second, ONTOLEX.canonicalForm, EX.homograph))
    for form, values in (
        (EX.singular, (Literal("cat", lang="en"), Literal("chat", lang="fr"))),
        (EX.plural, (Literal("cats", lang="en"),)),
        (EX.regional, (Literal("minou", lang="fr-CA"),)),
        (EX.homograph, (Literal("cat", lang="en"),)),
    ):
        graph.add((form, RDF.type, ONTOLEX.Form))
        for written in values:
            graph.add((form, ONTOLEX.writtenRep, written))
    graph.add((EX.plural, LI.number, LI.plural))
    for entry, sense, reference in (
        (EX.cat, EX.animalSense, EX.Cat),
        (EX.cat, EX.symbolSense, EX.symbol),
        (EX.second, EX.otherSense, EX.Cat),
    ):
        graph.add((entry, ONTOLEX.sense, sense))
        graph.add((sense, RDF.type, ONTOLEX.LexicalSense))
        graph.add((sense, ONTOLEX.reference, reference))
    annotation = BNode()
    graph.add((EX.animalSense, EX.usage, annotation))
    graph.add((annotation, RDFS.comment, Literal("retained extension", lang="en")))
    graph.add((EX.plain, SKOS.prefLabel, Literal("label only", lang="en")))
    return graph, VocabularyView(vocabulary), (EX.cat, EX.second)


def _read(
    graph: Graph | PreparedSource, view: VocabularyView, entries: tuple[URIRef, ...]
) -> Lexicon:
    """Keep ordinary public explicit entry selection visible in every control."""
    return read_lexicon(graph, entries, vocabulary=view)


def test_forms_two_senses_extensions_and_same_lemmas_round_trip_actual_rdf() -> None:
    """Native RDF serialization and parsing preserve full source graph semantics."""
    graph, view, entries = _consumer()
    lexicon = _read(graph, view, entries)
    assert lexicon.report.verified, lexicon.report.to_dict()
    entry = lexicon.entries[0]
    assert {s.identifier for s in entry.senses} == {EX.animalSense, EX.symbolSense}
    assert {r.target for s in entry.senses for r in s.references} == {EX.Cat, EX.symbol}
    symbol = next(
        r for s in entry.senses for r in s.references if r.target == EX.symbol
    )
    assert (
        symbol.declaration is not None and str(RDF.Property) in symbol.declaration.kinds
    )
    assert (
        lexicon.entries[0].forms[0].form.identifier
        != lexicon.entries[1].forms[0].form.identifier
    )
    exported = write_lexicon(lexicon)
    assert exported.report.verified and exported.source is not None
    payload = exported.source.graph().serialize(format="turtle")
    reloaded = prepare_rdf(payload, format="turtle")
    assert compare_rdf(prepare_rdf(graph), reloaded).verified
    again = _read(reloaded, view, entries)
    assert again.report.verified and len(again.entries[0].senses) == 2
    graph.remove((EX.cat, ONTOLEX.sense, EX.symbolSense))
    assert len(lexicon.entries[0].senses) == 2


def test_language_lookup_preserves_tied_entries_form_roles_and_senses() -> None:
    """Exact language wins, explicit role filtering permits truncation, ties survive."""
    graph, view, entries = _consumer()
    lexicon = _read(graph, view, entries)
    exact = lookup_written(lexicon, ranges=("fr-CA",))
    assert exact.report.verified and [str(m.written) for m in exact.matches] == [
        "minou"
    ]
    assert len(exact.matches[0].entry.senses) == 2
    fallback = lookup_written(lexicon, ranges=("fr-CA",), roles=("canonical",))
    assert [str(m.written) for m in fallback.matches] == ["chat"]
    tied = lookup_written(lexicon, ranges=("en",))
    assert len(tied.matches) == 2 and {m.entry.identifier for m in tied.matches} == set(
        entries
    )
    assert tied.matches[0].link.form.identifier != tied.matches[1].link.form.identifier
    absent = lookup_written(lexicon, ranges=("de",))
    assert not absent.matches and absent.report.complete
    allowed = lookup_written(lexicon, ranges=("de",), fallback=("other",))
    assert allowed.matches and allowed.report.verified


def test_only_explicit_cardinality_policy_rejects_distinct_asserted_resources() -> None:
    """Finite assertion counts never pretend that different IRIs prove OWL conflict."""
    graph, view, entries = _consumer()
    graph.add((EX.cat, ONTOLEX.canonicalForm, EX.plural))
    graph.add((EX.animalSense, ONTOLEX.reference, EX.symbol))
    graph.add((EX.second, ONTOLEX.sense, EX.animalSense))
    asserted = _read(graph, view, entries)
    assert asserted.report.verified
    assert next(
        s for s in asserted.entries[0].senses if s.identifier == EX.animalSense
    ) is next(s for s in asserted.entries[1].senses if s.identifier == EX.animalSense)
    checked = read_lexicon(
        graph,
        entries,
        vocabulary=view,
        policy=LexicalPolicy(
            canonical_minimum=1,
            canonical_maximum=1,
            reference_maximum=1,
            sense_owners_maximum=1,
        ),
    )
    assert checked.report.outcome == "disagreement"
    assert {i.code for i in checked.report.issues} >= {
        "ontolex.canonical_count",
        "ontolex.reference_count",
        "ontolex.sense_owner_count",
    }
    assert not write_lexicon(checked).report.verified


def test_required_references_extensions_and_plain_labels_cannot_gain_meaning() -> None:
    """A selected missing declaration and unknown required module remain visible."""
    graph, view, entries = _consumer()
    graph.add((EX.symbolSense, ONTOLEX.reference, EX.absent))
    dangling = _read(graph, view, entries)
    assert not dangling.report.verified and any(
        i.code == "ontolex.reference_dangling" for i in dangling.report.issues
    )
    assert any(
        r.target == EX.absent
        and r.declaration is not None
        and r.declaration.status == "absent"
        for s in dangling.entries[0].senses
        for r in s.references
    )
    graph.remove((EX.symbolSense, ONTOLEX.reference, EX.absent))
    unsupported = read_lexicon(
        graph,
        entries,
        vocabulary=view,
        policy=LexicalPolicy(required_extensions=(LI.number,)),
    )
    assert not unsupported.report.verified and unsupported.source is not None
    assert any(
        i.code == "ontolex.extension_unverified" for i in unsupported.report.issues
    )
    plain = _read(graph, view, (EX.plain,))
    assert not plain.report.verified and not plain.entries


def test_label_projection_retains_source_and_reports_structure_loss() -> None:
    """Labels target entry identities; ontology references never become synonyms."""
    graph, view, entries = _consumer()
    lexicon = _read(graph, view, entries)
    labels = project_labels(lexicon)
    assert labels.source is not None and not labels.report.verified and labels.omitted
    assert labels.lexicon is lexicon and labels.lexicon.source is lexicon.source
    projected = labels.source.graph()
    assert set(projected.subjects()) == set(entries)
    assert not list(projected.triples((EX.Cat, None, None)))
    assert any(
        o.statement == (EX.animalSense, ONTOLEX.reference, EX.Cat)
        for o in labels.omitted
    )
    assert not _read(labels.source, view, entries).report.verified


def test_selected_graph_owned_blanks_and_bounded_failures_remain_exact() -> None:
    """Other graphs, foreign labels and exhausted reads cannot satisfy selection."""
    graph, view, entries = _consumer()
    blank = BNode()
    graph.add((blank, RDF.type, ONTOLEX.LexicalEntry))
    graph.add((blank, ONTOLEX.canonicalForm, EX.singular))
    dataset = Dataset()
    for triple in graph:
        dataset.graph(EX.lexical).add(triple)
    dataset.graph(EX.other).add(
        (EX.singular, ONTOLEX.writtenRep, Literal("wrong", lang="en"))
    )
    dataset.graph(EX.empty)
    source = prepare_rdf(dataset)
    owned = next(
        s
        for s in source.graph(EX.lexical).subjects(RDF.type, ONTOLEX.LexicalEntry)
        if isinstance(s, BNode)
    )
    lexicon = read_lexicon(source, (owned,), vocabulary=view, graph_name=EX.lexical)
    assert lexicon.report.verified and lexicon.graph == EX.lexical
    assert "wrong" not in {
        str(v) for f in lexicon.entries[0].forms for v in f.form.written
    }
    exported = write_lexicon(lexicon)
    assert exported.source is not None and compare_rdf(source, exported.source).verified
    assert EX.empty in tuple(g.identifier for g in exported.source.dataset().graphs())
    assert not read_lexicon(
        source, (BNode(str(owned)),), vocabulary=view, graph_name=EX.lexical
    ).report.verified
    assert not read_lexicon(source, entries, vocabulary=view).report.verified
    stopped = read_lexicon(graph, entries, vocabulary=view, limits=Limits(max_work=1))
    assert stopped.report.outcome == "exhausted"
    event = Event()
    event.set()
    assert (
        read_lexicon(graph, entries, vocabulary=view, cancel=event).report.outcome
        == "exhausted"
    )
    graph.add((EX.symbolSense, ONTOLEX.reference, EX.absent))
    graph.add((EX.symbolSense, ONTOLEX.reference, EX.otherAbsent))
    budget = Budget(Limits(max_issues=1))
    partial = read_lexicon(graph, entries, vocabulary=view, budget=budget)
    assert partial.report.outcome == "exhausted" and any(
        i.code == "ontolex.reference_dangling" for i in partial.report.issues
    )


def test_full_export_retains_original_physical_pin_and_fresh_blank_scope() -> None:
    """Backing graphs survive without becoming part of the selected graph scope."""
    graph, view, entries = _consumer()
    for selection in (EX.lexical, "@default", BNode()):
        dataset = Dataset(default_union=False)
        lexical = (
            dataset.default_graph
            if selection == "@default"
            else dataset.graph(selection)
        )
        for triple in graph:
            lexical.add(triple)
        dataset.graph(EX.other).add((EX.hidden, EX.secret, Literal("retained")))
        dataset.graph(EX.empty)
        source = prepare_rdf(
            RdfSource(
                dataset,
                document_iri="urn:lexicon:document",
                effective_url="urn:lexicon:retrieval",
                graph_name=None if isinstance(selection, BNode) else selection,
            )
        )
        if isinstance(selection, BNode):
            source.graph_name = next(
                context.identifier
                for context in source.dataset().graphs()
                if isinstance(context.identifier, BNode)
            )
        lexicon = read_lexicon(source, entries, vocabulary=view)
        assert lexicon.report.verified, lexicon.report.to_dict()
        exported = write_lexicon(lexicon)
        assert exported.report.verified and exported.source is not None
        assert compare_rdf(source, exported.source).verified
        assert (
            exported.source.document_iri == source.document_iri
            and exported.source.effective_url == source.effective_url
            and exported.source.literal_fidelity == source.literal_fidelity
            and exported.source.graph_fidelity == source.graph_fidelity
        )
        names = {context.identifier for context in exported.source.dataset().graphs()}
        assert EX.other in names and EX.empty in names
        if isinstance(source.graph_name, BNode):
            assert isinstance(exported.source.graph_name, BNode)
            assert exported.source.graph_name is not source.graph_name
            assert any(
                context.identifier is exported.source.graph_name
                for context in exported.source.dataset().graphs()
            )
        else:
            assert exported.source.graph_name == source.graph_name
