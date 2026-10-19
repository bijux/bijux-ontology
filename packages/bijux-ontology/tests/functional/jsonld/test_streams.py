"""Independent framed JSON-LD documents retain lazy scoped checking truth."""

import gc
import json
import threading
import weakref
from collections.abc import Iterator

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.streams.checking import check_jsonld_records
from bijux_ontology.jsonld.streams.types import JSONLDRecord
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.contracts.preparation import PreparedModels
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.reports import Issue, Report

EX = Namespace("https://example.org/records/")


def test_lazy_records_reuse_context_and_keep_independent_blank_and_base_scopes() -> (
    None
):
    """No tail is prefetched and source snapshots do not accumulate in the stream."""
    pytest.importorskip("pyld")
    pulled: list[int] = []
    loads: list[str] = []
    weak: list[weakref.ReferenceType[PreparedSource]] = []
    context = {"@context": {"p": {"@id": str(EX.p), "@type": "@id"}}}

    def loader(url: str, options: dict[str, object]) -> dict[str, object]:
        loads.append(url)
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": context,
        }

    def records() -> Iterator[JSONLDRecord]:
        for index in range(200):
            pulled.append(index)
            yield JSONLDRecord(
                json.dumps(
                    {
                        "@context": "urn:context",
                        "@id": "_:same",
                        "p": "item",
                        str(EX.code): {"@value": "01", "@type": str(XSD.integer)},
                    }
                ),
                base=f"https://example.org/{index}/",
                document_iri=f"urn:record:{index}",
            )

    stream = check_jsonld_records(records(), document_loader=loader)
    initial = stream.report
    assert pulled == [] and not initial.complete and not initial.verified
    first = next(stream)
    assert pulled == [0] and first.report.verified
    assert first.preparation.source is not None
    left, _, target = next(first.preparation.source.graph().triples((None, EX.p, None)))
    original_literal = next(first.preparation.source.graph().objects(None, EX.code))
    assert isinstance(original_literal, Literal)
    assert str(original_literal) == "01" and original_literal.datatype == XSD.integer
    assert isinstance(left, BNode) and str(target) == "https://example.org/0/item"
    context["@context"]["p"]["@id"] = str(EX.changed)
    second = next(stream)
    assert second.preparation.source is not None
    right, _, other = next(
        second.preparation.source.graph().triples((None, EX.p, None))
    )
    assert isinstance(right, BNode) and left != right and left is not right
    assert str(other) == "https://example.org/1/item"
    weak.extend(
        (weakref.ref(first.preparation.source), weakref.ref(second.preparation.source))
    )
    del first, second
    for result in stream:
        assert result.report.verified and result.preparation.source is not None
        weak.append(weakref.ref(result.preparation.source))
        assert stream.retained_records == 1
    del result
    gc.collect()
    assert sum(reference() is not None for reference in weak) <= 1
    assert loads == ["urn:context"] and len(pulled) == 200
    assert stream.processed == 200 and stream.report.verified
    assert stream.report.exclusions == ("cross-record-obligations",)
    revised = check_jsonld_records(
        (JSONLDRecord('{"@context":"urn:context","@id":"urn:s","p":"urn:o"}'),),
        document_loader=loader,
    )
    changed = next(revised)
    assert changed.preparation.source is not None
    assert list(changed.preparation.source.graph().triples((None, EX.changed, None)))
    assert loads == ["urn:context", "urn:context"]
    revised.close()
    joint = next(
        check_jsonld_records(
            (
                JSONLDRecord(
                    '[{"@id":"_:same","urn:p":{"@id":"urn:a"}},'
                    ' {"@id":"_:same","urn:q":{"@id":"urn:b"}}]'
                ),
            )
        )
    )
    assert joint.preparation.source is not None
    graph = joint.preparation.source.graph()
    assert len(graph) == 2 and len(set(graph.subjects())) == 1
    relative = '{"@context":"urn:context","@id":"urn:s","p":"item"}'
    scoped = list(
        check_jsonld_records(
            (
                JSONLDRecord(relative, base="https://example.org/base/"),
                JSONLDRecord(relative),
            ),
            document_loader=loader,
        )
    )
    assert scoped[0].report.verified and not scoped[1].report.verified


def test_compiled_checks_retain_record_truth_before_malformed_tail() -> None:
    """A compiled declaration is reused while actual output and final syntax fail."""
    pytest.importorskip("pyld")
    vocabulary = Graph().add((EX.Record, RDF.type, OWL.Class))
    vocabulary.add((EX.label, RDF.type, OWL.DatatypeProperty))
    vocabulary.add((EX.label, RDFS.domain, EX.Record))
    vocabulary.add((EX.label, RDFS.range, XSD.string))
    plan = PreparedModels(
        (
            ModelDeclaration(
                "record",
                (str(EX.Record),),
                (
                    FieldDeclaration(
                        "label",
                        str(EX.label),
                        (ValueDeclaration("literal", datatype=str(XSD.string)),),
                        emission="guaranteed",
                    ),
                ),
            ),
        ),
        vocabulary,
    )
    assert plan.declaration_report.verified
    calls: list[PreparedSource] = []

    def check(source: PreparedSource, budget: Budget) -> Report:
        calls.append(source)
        return plan.check_rdf(source, (RdfModelUsage("record", EX.root),))

    valid = {
        "@context": {"@vocab": str(EX)},
        "@id": str(EX.root),
        "@type": "Record",
        "label": "actual",
    }
    invalid = {**valid, "label": 17}
    stream = check_jsonld_records(
        (
            JSONLDRecord(json.dumps(valid)),
            JSONLDRecord(json.dumps(invalid)),
            JSONLDRecord('{"final":'),
        ),
        check=check,
    )
    first = next(stream)
    assert first.report.verified
    second = next(stream)
    assert not second.report.verified and second.preparation.report.verified
    third = next(stream)
    assert third.report.outcome == "input_error" and third.preparation.source is None
    assert len(calls) == 2 and calls[0] is not calls[1]
    assert list(stream) == [] and stream.finished and not stream.report.verified
    assert first.report.verified and not second.report.verified
    assert any(issue.kind == "input_error" for issue in stream.report.issues)
    assert any(issue.kind == "finding" for issue in stream.report.issues)


def test_record_and_summary_limits_preserve_paid_truth() -> None:
    """Record policies apply before copies and summary limits keep raw paid results."""
    pytest.importorskip("pyld")
    ordinary = JSONLDRecord('{"@id":"urn:s","urn:p":{"@id":"urn:o"}}')
    limited = check_jsonld_records((ordinary,), record_limits=Limits(max_work=1))
    result = next(limited)
    assert result.report.outcome == "exhausted" and not limited.report.verified
    enormous = JSONLDRecord(ordinary.payload, base="urn:" + "x" * 100000)
    result = next(
        check_jsonld_records((enormous,), record_limits=Limits(max_bytes=1000))
    )
    assert result.report.outcome == "exhausted" and result.base is None
    assert all(len(issue.location) < 1000 for issue in result.report.issues)

    def mismatch(source: PreparedSource, budget: Budget) -> Report:
        issues = IssueCollector(budget)
        issues.append(
            Issue("records.actual_mismatch", "Actual record has a required mismatch")
        )
        return Report("actual-native-check", 1, tuple(issues))

    stream = check_jsonld_records(
        (ordinary, ordinary),
        limits=Limits(max_states=1),
        record_limits=Limits(),
        max_records=1,
        check=mismatch,
    )
    raw = next(stream)
    assert any(issue.code == "records.actual_mismatch" for issue in raw.report.issues)
    assert stream.report.outcome == "exhausted" and list(stream) == []
    assert stream.report.children == ()
    assert raw.report.outcome == "disagreement"

    def oversized(source: PreparedSource, budget: Budget) -> Report:
        issues = IssueCollector(budget)
        issues.append(Issue("records.paid_before_metadata", "Actual paid finding"))
        return Report("x" * 100000, 1, tuple(issues))

    bounded = next(
        check_jsonld_records(
            (ordinary,), record_limits=Limits(max_bytes=1000), check=oversized
        )
    )
    assert bounded.report.outcome == "exhausted"
    assert any(
        issue.code == "records.paid_before_metadata" for issue in bounded.report.issues
    )
    assert all(len(child.scope) < 1000 for child in bounded.report.children)

    def oversized_limit(source: PreparedSource, budget: Budget) -> Report:
        raise LimitExceeded("x" * 100000)

    bounded = next(
        check_jsonld_records(
            (ordinary,), record_limits=Limits(max_bytes=1000), check=oversized_limit
        )
    )
    assert bounded.report.outcome == "exhausted"
    assert all(len(issue.message) < 1000 for issue in bounded.report.issues)

    def loader_limit(url: str, options: dict[str, object]) -> dict[str, object]:
        raise LimitExceeded("x" * 100000)

    bounded = next(
        check_jsonld_records(
            (JSONLDRecord('{"@context":"urn:context","@id":"urn:s"}'),),
            document_loader=loader_limit,
            record_limits=Limits(max_bytes=1000),
        )
    )
    assert bounded.report.outcome == "exhausted" and bounded.preparation.source is None
    assert all(len(issue.message) < 1000 for issue in bounded.preparation.report.issues)
    assert all(
        len(issue.message) < 1000
        for child in bounded.report.children
        for issue in child.issues
    )


def test_cancel_close_and_restart_preserve_caller_producer() -> None:
    """Completed records survive cancellation while unseen records remain unchecked."""
    pytest.importorskip("pyld")
    frame = JSONLDRecord('{"@id":"urn:s","urn:p":{"@id":"urn:o"}}')

    class Producer(Iterator[JSONLDRecord]):
        def __init__(self) -> None:
            self.read = 0
            self.closed = False

        def __next__(self) -> JSONLDRecord:
            self.read += 1
            return frame

        def close(self) -> None:
            self.closed = True

    producer = Producer()
    cancellation = threading.Event()

    def check(source: PreparedSource, budget: Budget) -> Report:
        cancellation.set()
        return Report("actual-record", 1)

    stream = check_jsonld_records(producer, check=check, cancel=cancellation)
    first = next(stream)
    assert first.report.verified and producer.read == 1
    assert stream.last_result is first
    assert list(stream) == [] and stream.report.outcome == "exhausted"
    stream.close()
    assert not producer.closed and first.report.verified
    fresh = check_jsonld_records((frame,))
    assert next(fresh).report.verified and list(fresh) == [] and fresh.report.verified
    partial = check_jsonld_records(producer)
    assert next(partial).report.verified
    partial.close()
    assert partial.report.outcome == "exhausted" and not producer.closed
    capped = check_jsonld_records(producer, max_records=1)
    assert next(capped).report.verified
    count = producer.read
    assert list(capped) == [] and producer.read == count + 1
    assert capped.report.outcome == "exhausted"


def test_record_framing_is_explicit_and_arbitrary_callback_failures_propagate() -> None:
    """Neither malformed frames nor application failures manufacture completion."""
    with pytest.raises(InputError):
        JSONLDRecord({"unframed": True})  # type: ignore[arg-type]
    with pytest.raises(InputError):
        check_jsonld_records("not a record iterator")  # type: ignore[arg-type]
    pytest.importorskip("pyld")
    stream = check_jsonld_records((object(),))  # type: ignore[arg-type]
    assert next(stream).report.outcome == "input_error"
    assert list(stream) == [] and not stream.report.verified
    empty = check_jsonld_records(())
    assert list(empty) == [] and empty.finished and not empty.report.verified

    attempts = []

    def fail(source: PreparedSource, budget: Budget) -> Report:
        attempts.append(source)
        if len(attempts) == 1:
            raise RuntimeError("ordinary caller failure")
        return Report("later-record", 1)

    frame = JSONLDRecord('{"@id":"urn:s"}')
    stream = check_jsonld_records((frame, frame), check=fail)
    with pytest.raises(RuntimeError, match="ordinary caller failure"):
        next(stream)
    assert not stream.report.complete and not stream.report.verified
    assert list(stream) == [] and len(attempts) == 1
    stream.close()
