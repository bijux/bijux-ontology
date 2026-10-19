"""Caller-managed reads use actual native transactions and ordinary checkers."""

import pyoxigraph as ox
import pytest
import rdflib
from rdflib import BNode, Graph, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.stores.external.preparation import prepare_external_store
from bijux_ontology.rdf.stores.external.queries import query_external
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.vocabulary.external import check_external_terms


def _store() -> ox.Store:
    store = ox.Store()
    store.add(
        ox.Quad(
            ox.NamedNode("urn:Original"),
            ox.NamedNode(str(RDF.type)),
            ox.NamedNode(str(OWL.Class)),
        )
    )
    return store


def test_actual_iterator_snapshot_survives_mutation_and_next_read_changes() -> None:
    """Native reads own a transaction before Python captures their rows."""
    store = _store()
    iterator = store.query("SELECT ?s WHERE {?s ?p ?o}")
    assert isinstance(iterator, ox.QuerySolutions)
    store.clear()
    store.add(
        ox.Quad(
            ox.NamedNode("urn:Later"),
            ox.NamedNode(str(RDF.type)),
            ox.NamedNode(str(OWL.Class)),
        )
    )
    assert [row[0] for row in iterator] == [ox.NamedNode("urn:Original")]
    source = prepare_external_store(store)
    current = query_external(
        source, "SELECT ?s WHERE {?s ?p ?o}", scope=QueryScope("default")
    )
    assert current.report.verified and current.rows == ((URIRef("urn:Later"),),)
    assert source.isolation == "single-native-read"
    store.clear()
    later = query_external(source, "ASK {?s ?p ?o}", scope=QueryScope("default"))
    assert later.report.verified and later.answer is False
    assert current.rows == ((URIRef("urn:Later"),),)


def test_selected_native_declarations_reuse_ordinary_kind_checks() -> None:
    """A bounded projection cannot need ownership of all unrelated data."""
    store = _store()
    for index in range(100):
        store.add(
            ox.Quad(
                ox.NamedNode(f"urn:unrelated:{index}"),
                ox.NamedNode("urn:p"),
                ox.Literal("original"),
            )
        )
    source = prepare_external_store(store)
    report = check_external_terms(
        source,
        (TermDeclaration("urn:Original", (str(OWL.Class),)),),
        scope=QueryScope("default"),
        limits=Limits(max_triples=10),
    )
    assert report.verified
    missing = check_external_terms(
        source, (TermDeclaration("urn:Missing"),), scope=QueryScope("default")
    )
    assert any(issue.code == "vocabulary.term_missing" for issue in missing.issues)
    wrong = check_external_terms(
        source,
        (TermDeclaration("urn:Original", (str(OWL.ObjectProperty),)),),
        scope=QueryScope("default"),
    )
    assert any(
        issue.code == "vocabulary.entity_kind_mismatch" for issue in wrong.issues
    )


def test_physical_native_scope_and_original_supported_terms() -> None:
    """Hidden named values cannot fill a default graph declaration or literal."""
    store = _store()
    store.add(
        ox.Quad(
            ox.NamedNode("urn:Hidden"),
            ox.NamedNode(str(RDF.type)),
            ox.NamedNode(str(OWL.ObjectProperty)),
            ox.NamedNode("urn:g"),
        )
    )
    store.add(
        ox.Quad(
            ox.NamedNode("urn:s"),
            ox.NamedNode("urn:p"),
            ox.Literal("line\ntext", datatype=ox.NamedNode(str(XSD.string))),
            ox.NamedNode("urn:g"),
        )
    )
    store.add_graph(ox.NamedNode("urn:empty"))
    source = prepare_external_store(store)
    for scope, found in (
        (QueryScope("default"), False),
        (QueryScope("union"), True),
    ):
        result = query_external(source, "ASK {<urn:Hidden> ?p ?o}", scope=scope)
        assert result.report.verified and result.answer is found
    result = query_external(
        source,
        "SELECT ?o WHERE {GRAPH <urn:g> {<urn:s> <urn:p> ?o}}",
        scope=QueryScope("dataset"),
    )
    assert result.rows == ((literal_term("line\ntext", datatype=str(XSD.string)),),)
    inventory = query_external(
        source, "SELECT ?g WHERE {GRAPH ?g {}}", scope=QueryScope("dataset")
    )
    assert inventory.report.verified
    assert (URIRef("urn:empty"),) in inventory.rows
    unsupported = query_external(
        source, "ASK {?s ?p ?o}", scope=QueryScope("named", "urn:g")
    )
    assert unsupported.report.outcome == "unverified"


def test_unstable_handles_retrieval_and_hard_deadlines_are_not_verified() -> None:
    """Unsupported guarantees are rejected before the caller store is queried."""
    with pytest.raises(InputError):
        prepare_external_store(Graph())  # type: ignore[arg-type]
    store = _store()
    source = prepare_external_store(store)
    deadline = query_external(
        source, "ASK {?s ?p ?o}", scope=QueryScope("default"), timeout=1
    )
    assert not deadline.report.verified
    blank = query_external(
        source, "ASK {?s ?p ?o}", scope=QueryScope("named", BNode("g"))
    )
    assert not blank.report.verified
    with pytest.raises(LimitExceeded):
        query_external(
            source,
            "ASK {?s ?p ?o}",
            scope=QueryScope("named", "urn:" + "x" * 10000),
            limits=Limits(max_bytes=1000),
        )
    remote = query_external(
        source,
        "ASK { SERVICE <https://invalid.example> {?s ?p ?o} }",
        scope=QueryScope("default"),
    )
    assert not remote.report.verified


def test_cumulative_bounds_and_completed_negative_findings() -> None:
    """No row truncation, tiny budget or later exhaustion becomes agreement."""
    store = _store()
    store.add(
        ox.Quad(
            ox.NamedNode("urn:Second"),
            ox.NamedNode(str(RDF.type)),
            ox.NamedNode(str(OWL.Class)),
        )
    )
    source = prepare_external_store(store)
    limited = query_external(
        source, "SELECT ?s WHERE {?s ?p ?o}", scope=QueryScope("default"), max_rows=1
    )
    assert limited.report.outcome == "exhausted" and len(limited.rows) == 1
    tiny = query_external(
        source, "ASK {?s ?p ?o}", scope=QueryScope("default"), limits=Limits(max_work=1)
    )
    assert tiny.report.outcome == "exhausted"
    operation = Budget(Limits(max_work=2000))
    seen = []
    for _ in range(20):
        report = check_external_terms(
            source,
            (TermDeclaration("urn:Missing"),),
            scope=QueryScope("default"),
            budget=operation,
        )
        seen.append(report)
        if report.outcome == "exhausted":
            break
    assert any(any(i.code == "vocabulary.term_missing" for i in r.issues) for r in seen)
    assert seen[-1].outcome == "exhausted"


def test_query_literals_keep_caller_normalization_policy_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing parser qualification rejects native lexical loss in either mode."""
    source = prepare_external_store(_store())
    for flag in (False, True):
        monkeypatch.setattr(rdflib, "NORMALIZE_LITERALS", flag)
        canonical = query_external(
            source,
            'SELECT ("1"^^<http://www.w3.org/2001/XMLSchema#integer> AS ?v) WHERE {}',
            scope=QueryScope("default"),
        )
        assert canonical.report.verified
        assert canonical.rows == ((literal_term("1", datatype=str(XSD.integer)),),)
        unsupported = query_external(
            source,
            'SELECT ("01"^^<http://www.w3.org/2001/XMLSchema#integer> AS ?v) WHERE {}',
            scope=QueryScope("default"),
        )
        assert unsupported.report.outcome == "unverified"
        assert rdflib.NORMALIZE_LITERALS is flag
