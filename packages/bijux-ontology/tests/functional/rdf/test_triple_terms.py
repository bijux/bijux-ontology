"""Versioned maintained triple terms retain descriptions without asserting them."""

import hashlib
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import cast

import pyoxigraph as ox
import pytest
from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.canonical.export import canonicalize_rdf
from bijux_ontology.rdf.entailment.preparation import prepare_entailment
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.triple_terms import (
    TRIPLE_TERM_CONTRACT,
    TripleTermSnapshot,
    export_triple_terms,
    inspect_triple_terms,
    prepare_triple_terms,
)
from bijux_ontology.shacl.checking import validate_shapes

_SOURCE = (
    "<urn:s> <urn:p> <<( _:shared <urn:q> <<( _:shared <urn:r> "
    '"01"^^<http://www.w3.org/2001/XMLSchema#integer> )>> )>> .'
)


def _prepared() -> TripleTermSnapshot:
    result = prepare_triple_terms(
        RdfSource(_SOURCE, format="turtle"), contract=TRIPLE_TERM_CONTRACT
    )
    assert result.report.verified
    assert result.snapshot is not None
    return result.snapshot


def test_nested_owned_terms_without_asserted_descriptions() -> None:
    snapshot = _prepared()
    assert len(snapshot.quads) == 1
    quad = snapshot.quads[0]
    assert isinstance(quad.object, ox.Triple)
    outer = quad.object
    assert isinstance(outer.object, ox.Triple)
    inner = outer.object
    assert outer.subject == inner.subject
    assert isinstance(inner.object, ox.Literal)
    assert inner.object.value == "01"
    assert snapshot.source_sha256 == hashlib.sha256(_SOURCE.encode()).hexdigest()
    inspected = inspect_triple_terms(snapshot)
    assert inspected.report.verified
    assert inspected.triple_terms == (outer, inner)
    independent = _prepared()
    assert snapshot.quads[0].object != independent.quads[0].object
    with pytest.raises(InputError):
        TripleTermSnapshot()
    with pytest.raises(TypeError):
        replace(snapshot, source_sha256="0" * 64)


def test_exact_export_is_explicit_versioned_and_reparsed() -> None:
    snapshot = _prepared()
    result = export_triple_terms(snapshot)
    assert result.report.verified and result.content is not None
    assert b'"01"^^' in result.content
    reparsed = tuple(ox.parse(result.content, ox.RdfFormat.N_TRIPLES))
    assert reparsed == snapshot.quads
    assert len(reparsed) == 1
    for format in ("nt", "turtle", "xml", "json-ld"):
        unsupported = export_triple_terms(snapshot, format=format)
        assert not unsupported.report.verified and unsupported.content is None
    with pytest.raises(InputError):
        prepare_rdf(RdfSource(result.content, format="nt"))


def test_rdf11_consumers_reject_the_disjoint_snapshot() -> None:
    snapshot = _prepared()
    unsupported = cast(PreparedSource, snapshot)
    with pytest.raises(InputError):
        export_rdf(unsupported, format="nt")
    canonical = canonicalize_rdf(unsupported)
    assert not canonical.report.verified and canonical.content is None
    with pytest.raises(InputError):
        validate_shapes(unsupported, Graph())
    reasoning = prepare_entailment(unsupported, regime="rdfs")
    assert not reasoning.report.verified and reasoning.effective is None


def test_profile_scope_and_original_resource_metadata(tmp_path: Path) -> None:
    content = '<relative> <urn:p> <<( <urn:a> <urn:b> "quoted \\"text\\""@en )>> .'
    path = tmp_path / "terms.ttl"
    path.write_text(content)
    result = prepare_triple_terms(
        RdfSource(
            path,
            format="turtle",
            document_iri="https://example.org/logical",
            effective_url="https://example.org/actual/source",
        ),
        contract=TRIPLE_TERM_CONTRACT,
    )
    assert result.report.verified and result.snapshot is not None
    assert result.snapshot.quads[0].subject == ox.NamedNode(
        "https://example.org/actual/relative"
    )
    assert result.snapshot.document_iri == "https://example.org/logical"
    assert result.snapshot.effective_url == "https://example.org/actual/source"
    for source in (
        RdfSource(_SOURCE, format="trig"),
        RdfSource(_SOURCE, format="turtle", union=True),
        RdfSource(_SOURCE, format="turtle", graph_name="urn:g"),
        RdfSource(
            '<urn:s> <urn:p> <<( <urn:a> <urn:b> "x"@en--ltr )>> .', format="turtle"
        ),
        RdfSource('<urn:s> <urn:p> <<( "bad" <urn:b> <urn:c> )>> .', format="turtle"),
    ):
        unsupported = prepare_triple_terms(source, contract=TRIPLE_TERM_CONTRACT)
        assert not unsupported.report.verified and unsupported.snapshot is None
    wrong = prepare_triple_terms(RdfSource(_SOURCE, format="turtle"), contract="rdf11")
    assert not wrong.report.verified and wrong.snapshot is None
    assert wrong.report.outcome == "unverified"
    malformed = prepare_triple_terms(
        RdfSource("<urn:s> <urn:p> this is malformed .", format="turtle"),
        contract=TRIPLE_TERM_CONTRACT,
    )
    assert malformed.snapshot is None and malformed.report.outcome == "input_error"
    assert [(issue.code, issue.kind) for issue in malformed.report.issues] == [
        ("input.invalid", "input_error")
    ]


def test_cumulative_limits_cancel_and_output_atomicity() -> None:
    snapshot = _prepared()
    for limits in (
        Limits(max_bytes=10),
        Limits(max_states=1),
        Limits(max_work=1),
        Limits(max_depth=1),
        Limits(max_triples=1),
    ):
        prepared = prepare_triple_terms(
            RdfSource(_SOURCE, format="turtle"),
            contract=TRIPLE_TERM_CONTRACT,
            limits=limits,
        )
        assert prepared.report.outcome == "exhausted" and prepared.snapshot is None
        exported = export_triple_terms(snapshot, limits=limits)
        assert exported.report.outcome == "exhausted" and exported.content is None
    quota = Budget(Limits(max_issues=1))
    quota.consume("issues")
    spent = prepare_triple_terms(
        RdfSource(_SOURCE, format="trig"), contract=TRIPLE_TERM_CONTRACT, budget=quota
    )
    assert spent.report.outcome == "exhausted" and spent.snapshot is None
    event = Event()
    event.set()
    cancelled = export_triple_terms(snapshot, cancel=event)
    assert cancelled.report.outcome == "exhausted" and cancelled.content is None
    shared = Budget()
    first = export_triple_terms(snapshot, budget=shared)
    assert first.report.verified and shared.remaining("work") < shared.limits.max_work
    shared.consume("work", shared.remaining("work"))
    second = export_triple_terms(snapshot, budget=shared)
    assert second.report.outcome == "exhausted" and second.content is None
    assert export_triple_terms(snapshot).report.verified
