"""Actual native disk snapshots retain exact selected terms and bounded ownership."""

from pathlib import Path
from threading import Event

import pyoxigraph as ox
from rdflib import BNode, Dataset, Namespace
from rdflib.namespace import OWL

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.queries.access import QueryScope, query_rdf
from bijux_ontology.rdf.stores import (
    materialize_snapshot,
    open_snapshot,
    query_snapshot,
    snapshot_rdf,
)
from bijux_ontology.reports import Issue
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.inspection import VocabularyView
from bijux_ontology.vocabulary.storage import check_stored_terms, inspect_stored_terms

E = Namespace("urn:store:")


def _source(directory: Path) -> tuple[Path, Dataset]:
    """Keep graph/data sharing and datatype lexicals in one ordinary RDF fixture."""
    source = directory / "input.nq"
    source.write_text(
        "<urn:store:A> <http://www.w3.org/1999/02/22-rdf-syntax-ns#type> "
        "<http://www.w3.org/2002/07/owl#Class> .\n"
        '<urn:store:A> <urn:store:label> "Alpha"@EN .\n'
        '<urn:store:A> <urn:store:opaque> "opaque"^^<urn:store:Datatype> .\n'
        '<urn:store:record> <urn:store:value> "1"'
        "^^<http://www.w3.org/2001/XMLSchema#integer> <urn:store:primary> .\n"
        "<urn:store:record> <urn:store:left> _:shared <urn:store:primary> .\n"
        "<urn:store:record> <urn:store:right> _:shared <urn:store:primary> .\n"
        "_:shared <urn:store:edge> _:other _:shared .\n"
        '<urn:store:Hidden> <urn:store:label> "hidden" <urn:store:hidden> .\n'
    )
    expected = prepare_rdf(source.read_bytes(), format="nquads").dataset()
    return source, expected


def test_native_scopes_match_memory_queries_and_explicit_materialization(
    tmp_path: Path,
) -> None:
    source, expected = _source(tmp_path)
    result = snapshot_rdf(source, tmp_path / "snapshot", format="nquads")
    assert result.report.verified and result.snapshot is not None
    snapshot = result.snapshot
    for scope in (
        QueryScope("default"),
        QueryScope("named", E.primary),
        QueryScope("union"),
    ):
        text = (
            "SELECT ?s ?p ?o WHERE { ?s ?p ?o "
            "FILTER(isIRI(?s) && isLiteral(?o)) } ORDER BY ?s ?p ?o"
        )
        disk = query_snapshot(snapshot, text, scope=scope)
        memory = query_rdf(expected, text, scope=scope)
        assert disk.report.verified and memory.report.verified
        assert disk.variables == memory.variables and disk.rows == memory.rows
        materialized = materialize_snapshot(snapshot, scope=scope, max_triples=20)
        assert materialized.report.verified and materialized.source is not None
    whole = materialize_snapshot(snapshot, scope=QueryScope("dataset"), max_triples=20)
    assert whole.report.verified and whole.source is not None
    assert compare_rdf(whole.source, expected, ignore_empty_graphs=True).verified
    assert not query_snapshot(
        snapshot, "ASK {}", scope=QueryScope("dataset")
    ).report.verified
    budget = Budget(Limits(max_issues=1))
    IssueCollector(budget).append(Issue("input.invalid", "Already owned finding"))
    exhausted = query_snapshot(
        snapshot, "ASK {}", scope=QueryScope("dataset"), budget=budget
    )
    assert exhausted.report.outcome == "exhausted"
    limited = materialize_snapshot(snapshot, scope=QueryScope("union"), max_triples=1)
    assert limited.source is None and limited.report.outcome == "exhausted"
    leaked = query_snapshot(
        snapshot,
        "ASK { GRAPH <urn:store:hidden> { ?s ?p ?o } }",
        scope=QueryScope("named", E.primary),
    )
    assert leaked.report.verified and leaked.answer is False
    pin = next(name for name in snapshot.graph_names if isinstance(name, BNode))
    blank = query_snapshot(
        snapshot, "ASK { ?s <urn:store:edge> ?o }", scope=QueryScope("named", pin)
    )
    assert blank.report.verified and blank.answer is True
    assert not query_snapshot(
        snapshot, "ASK {}", scope=QueryScope("named", BNode(str(pin)))
    ).report.verified


def test_restart_input_and_store_mutation_cannot_change_active_snapshot(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    result = snapshot_rdf(source, tmp_path / "snapshot", format="nquads")
    assert result.snapshot is not None
    snapshot = result.snapshot
    restarted = open_snapshot(tmp_path / "snapshot")
    assert restarted.report.verified and restarted.snapshot is not None
    assert restarted.snapshot.revision == snapshot.revision
    source.write_text("<urn:changed> <urn:p> <urn:o> .")
    answer = query_snapshot(
        snapshot, "ASK { <urn:store:A> ?p ?o }", scope=QueryScope("default")
    )
    assert answer.report.verified and answer.answer is True
    mutable = ox.Store(str(snapshot.directory))
    mutable.add(
        ox.Quad(
            ox.NamedNode("urn:changed"), ox.NamedNode("urn:p"), ox.NamedNode("urn:o")
        )
    )
    mutable.flush()
    del mutable
    changed = query_snapshot(snapshot, "ASK {}", scope=QueryScope("default"))
    assert not changed.report.verified and changed.answer is None
    assert not open_snapshot(tmp_path / "snapshot").report.verified
    assert (tmp_path / "snapshot" / "manifest.json").exists()
    refused = snapshot_rdf(source, tmp_path / "snapshot")
    assert (
        not refused.report.verified
        and (tmp_path / "snapshot" / "manifest.json").exists()
    )


def test_noncanonical_literals_and_unsafe_queries_never_verify_changed_terms(
    tmp_path: Path,
) -> None:
    source = tmp_path / "literal.nt"
    source.write_text(
        '<urn:a> <urn:p> "01"^^<http://www.w3.org/2001/XMLSchema#integer> .'
    )
    result = snapshot_rdf(source, tmp_path / "bad")
    assert not result.report.verified and result.snapshot is None
    assert not (tmp_path / "bad").exists()
    source.write_text(
        '<urn:a> <urn:p> "1"^^<http://www.w3.org/2001/XMLSchema#integer> .'
    )
    good = snapshot_rdf(source, tmp_path / "good")
    assert good.snapshot is not None
    for query in (
        "ASK { SERVICE <https://example.invalid/> { ?s ?p ?o } }",
        "SELECT ?s FROM <https://example.invalid/> WHERE { ?s ?p ?o }",
        "INSERT DATA { <urn:a> <urn:p> <urn:o> }",
        'ASK { FILTER(sameTerm("01"^^<http://www.w3.org/2001/XMLSchema#integer>, '
        '"1"^^<http://www.w3.org/2001/XMLSchema#integer>)) }',
    ):
        assert not query_snapshot(
            good.snapshot, query, scope=QueryScope("default")
        ).report.verified
    short = query_snapshot(
        good.snapshot, "ASK {}", scope=QueryScope("default"), timeout=0.000001
    )
    assert short.report.outcome == "exhausted"
    cancelled = Event()
    cancelled.set()
    assert (
        query_snapshot(
            good.snapshot, "ASK {}", scope=QueryScope("default"), cancel=cancelled
        ).report.outcome
        == "exhausted"
    )
    assert query_snapshot(
        good.snapshot, "ASK {}", scope=QueryScope("default")
    ).report.verified


def test_selected_inspection_and_used_term_checks_reuse_existing_truth(
    tmp_path: Path,
) -> None:
    source, expected = _source(tmp_path)
    made = snapshot_rdf(source, tmp_path / "snapshot", format="nquads")
    assert made.snapshot is not None
    iris = (str(E.A), str(E.label), str(E.absent), str(E.Datatype))
    selected = inspect_stored_terms(made.snapshot, iris, scope=QueryScope("default"))
    assert selected.report.verified
    view = VocabularyView(expected.default_graph)
    assert selected.items == tuple(view.lookup(iri) for iri in iris)
    terms = (
        TermDeclaration(str(E.A), (str(OWL.Class),)),
        TermDeclaration(str(E.absent)),
    )
    actual = check_stored_terms(made.snapshot, terms, scope=QueryScope("default"))
    oracle = check_vocabulary(expected.default_graph, terms)
    assert actual.outcome == oracle.outcome == "disagreement"
    assert [(i.code, i.actual) for i in actual.issues] == [
        (i.code, i.actual) for i in oracle.issues
    ]


def test_selected_inventory_capture_exhaustion_is_a_structured_result(
    tmp_path: Path,
) -> None:
    """A small allowance stops public capture before any unbounded input adoption."""
    source, _ = _source(tmp_path)
    made = snapshot_rdf(source, tmp_path / "snapshot", format="nquads")
    assert made.snapshot is not None
    inspected = inspect_stored_terms(
        made.snapshot,
        (str(E.A), str(E.absent)),
        scope=QueryScope("default"),
        limits=Limits(max_states=1),
    )
    checked = check_stored_terms(
        made.snapshot,
        (TermDeclaration(str(E.A)), TermDeclaration(str(E.absent))),
        scope=QueryScope("default"),
        limits=Limits(max_states=1),
    )
    assert inspected.report.outcome == checked.outcome == "exhausted"


def test_reserved_named_graph_never_collapses_into_materialized_default(
    tmp_path: Path,
) -> None:
    """A native graph identity unsupported by RDFLib remains an explicit boundary."""
    source = tmp_path / "reserved.nq"
    source.write_text(
        '<urn:a> <urn:p> "default" .\n'
        '<urn:a> <urn:p> "named" <urn:x-rdflib:default> .\n'
    )
    made = snapshot_rdf(source, tmp_path / "snapshot", format="nquads")
    assert made.report.verified and made.snapshot is not None
    named = query_snapshot(
        made.snapshot,
        "SELECT ?o { GRAPH <urn:x-rdflib:default> { <urn:a> <urn:p> ?o } }",
        scope=QueryScope("dataset"),
    )
    assert not named.report.verified and str(named.rows[0][0]) == "named"
    whole = materialize_snapshot(
        made.snapshot, scope=QueryScope("dataset"), max_triples=10
    )
    assert not whole.report.verified and whole.source is None
    assert materialize_snapshot(
        made.snapshot, scope=QueryScope("default"), max_triples=10
    ).report.verified
