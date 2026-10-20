"""Actual native streaming checks retain local evidence without global claims."""

import hashlib
import importlib.util
from pathlib import Path
from threading import Event

from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SH

from bijux_ontology.data.streams import check_rdf_stream
from bijux_ontology.data.usage import check_data_usage
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.sources.loading import prepare_rdf
from bijux_ontology.rdf.sources.types import RdfSource
from bijux_ontology.rdf.stores.preparation import snapshot_rdf

E = Namespace("urn:stream:")
INTEGER = "http://www.w3.org/2001/XMLSchema#integer"


def _vocabulary() -> Graph:
    """Use only actual modest declarations required by the streamed positions."""
    return (
        Graph()
        .add((E.p, RDF.type, OWL.DatatypeProperty))
        .add((E.C, RDF.type, OWL.Class))
    )


def test_small_native_oracle_exact_literals_and_independent_global_scope(
    tmp_path: Path,
) -> None:
    source = tmp_path / "samples.nt"
    raw = (
        f'<urn:stream:s> <urn:stream:p> "01"^^<{INTEGER}> .\n'
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
        f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
        f'<urn:stream:s> <urn:stream:p> "1.5"^^<{INTEGER}> .\n'
    ).encode()
    source.write_bytes(raw)
    descriptor = RdfSource(
        source,
        format="nt",
        document_iri="https://example.test/data",
        effective_url="https://example.test/retrieved",
        role="data",
    )
    result = check_rdf_stream(descriptor, _vocabulary())
    ordinary = check_data_usage(prepare_rdf(descriptor), _vocabulary())
    assert sorted((i.code, i.actual) for i in result.report.issues) == sorted(
        (i.code, i.actual) for i in ordinary.issues
    )
    assert result.parse_report.verified and result.local_report.complete
    assert result.report.outcome == "disagreement" and result.statements == 4
    assert (
        result.bytes_read == len(raw)
        and result.source_sha256 == hashlib.sha256(raw).hexdigest()
    )
    assert (
        result.document_iri == descriptor.document_iri
        and result.effective_url == descriptor.effective_url
    )
    assert [f.statement.index for f in result.findings] == [2, 3, 4]
    assert isinstance(result.findings[-1].statement.value, Literal)
    assert str(result.findings[-1].statement.value) == "1.5"
    source.write_text("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n")
    scoped = check_rdf_stream(
        source, _vocabulary(), required_global=("shapes", "identity", "cardinality")
    )
    assert scoped.parse_report.verified and scoped.local_report.verified
    assert scoped.global_report is not None and not scoped.global_report.verified
    assert not scoped.report.verified
    if importlib.util.find_spec("pyshacl") is not None:
        from bijux_ontology.shacl.checking import validate_shapes

        shape = (
            Graph()
            .add((E.Shape, RDF.type, SH.NodeShape))
            .add((E.Shape, SH.targetNode, E.s))
        )
        shape.add((E.Shape, SH.property, E.Property)).add(
            (E.Property, SH.path, E.required)
        ).add((E.Property, SH.minCount, Literal(1)))
        assert validate_shapes(prepare_rdf(source), shape).conforms is False


def test_large_actual_stream_finds_late_invalid_positions_without_retaining_rows(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.nt"
    digest = hashlib.sha256()
    with source.open("wb") as stream:
        for index in range(20000):
            row = (
                f"<urn:stream:s{index}> <urn:stream:p> <urn:stream:o{index}> .\n"
            ).encode()
            stream.write(row)
            digest.update(row)
        tail = (
            "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
            f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
            f'<urn:stream:s> <urn:stream:p> "fraction"^^<{INTEGER}> .\n'
        ).encode()
        stream.write(tail)
        digest.update(tail)
    limits = Limits(
        max_bytes=2048,
        max_total_bytes=200000000,
        max_work=50000000,
        max_states=2000000,
        max_triples=100000,
    )
    result = check_rdf_stream(source, _vocabulary(), limits=limits)
    assert result.parse_report.verified and result.statements == 20003
    assert (
        result.report.outcome == "disagreement"
        and result.source_sha256 == digest.hexdigest()
    )
    assert len(result.findings) == 3
    assert [row.statement.index for row in result.findings] == [20001, 20002, 20003]
    assert [row.issue.code for row in result.findings] == [
        "vocabulary.term_missing",
        "vocabulary.term_missing",
        "literal.lexical_invalid",
    ]
    assert not hasattr(result, "dataset") and not hasattr(result, "graph")


def test_nquads_exact_graph_roles_and_retained_joint_blank_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "graphs.nq"
    source.write_text(
        "_:shared <urn:stream:missing> _:target _:shared .\n"
        "_:target <urn:stream:missing> _:shared _:shared .\n"
        f'<urn:stream:s> <urn:stream:p> "01"^^<{INTEGER}> <urn:stream:g> .\n'
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(source, _vocabulary())
    assert result.parse_report.verified and len(result.findings) == 3
    first, second, default = (row.statement for row in result.findings)
    assert isinstance(first.graph, BNode) and first.subject is first.graph
    assert second.value is first.graph and second.subject is first.value
    assert default.graph is None
    named = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name=str(E.g)), _vocabulary()
    )
    assert named.report.verified and named.statements == 4 and named.selected == 1
    selected_default = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name="@default"), _vocabulary()
    )
    assert (
        selected_default.report.outcome == "disagreement"
        and selected_default.selected == 1
    )
    assert selected_default.findings[0].statement.graph is None
    absent = check_rdf_stream(
        RdfSource(source, format="nquads", graph_name=str(E.absent)), _vocabulary()
    )
    assert absent.parse_report.verified and not absent.local_report.verified
    assert not any(issue.kind == "finding" for issue in absent.report.issues)
    duplicate = tmp_path / "duplicates.nt"
    duplicate.write_text("<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n" * 2)
    checked = check_rdf_stream(duplicate, _vocabulary())
    assert checked.report.verified and checked.statements == 2
    assert (
        check_rdf_stream(
            RdfSource(source, format="nquads", union=True), _vocabulary()
        ).report.verified
        is False
    )


def test_late_parse_issue_limits_and_cancellation_keep_honest_prefix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.nt"
    bad = "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    valid = "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
    source.write_text(bad + "<urn:stream:s> <urn:stream:p> broken .\n")
    partial = check_rdf_stream(source, _vocabulary())
    assert (
        not partial.parse_report.complete
        and partial.findings[0].issue.code == "vocabulary.term_missing"
    )
    failures = [
        i.details.failure
        for i in partial.parse_report.issues
        if i.details is not None and i.details.failure is not None
    ]
    assert (
        failures and failures[0].span is not None and failures[0].span.start_line == 2
    )
    source.write_text(bad * 2)
    capped = check_rdf_stream(source, _vocabulary(), limits=Limits(max_issues=1))
    assert capped.report.outcome == "exhausted" and not capped.parse_report.complete
    assert len(capped.findings) == 1 and any(
        i.code == "vocabulary.term_missing" for i in capped.report.issues
    )
    source.write_text(bad + valid * 100)
    byte_cap = check_rdf_stream(source, _vocabulary(), max_stream_bytes=1000)
    assert (
        byte_cap.report.outcome == "exhausted"
        and byte_cap.findings[0].statement.index == 1
    )
    source.write_text(bad + '<urn:stream:s> <urn:stream:p> "' + "x" * 2000 + '" .\n')
    record_cap = check_rdf_stream(source, _vocabulary(), limits=Limits(max_bytes=512))
    assert (
        record_cap.report.outcome == "exhausted"
        and record_cap.findings[0].statement.index == 1
    )
    event = Event()
    event.set()
    cancelled = check_rdf_stream(source, _vocabulary(), cancel=event)
    assert cancelled.report.outcome == "exhausted"
    source.write_text(valid)
    assert check_rdf_stream(source, _vocabulary()).report.verified
    assert (
        check_rdf_stream(source, _vocabulary(), timeout=0.001).report.outcome
        == "exhausted"
    )
    huge = RdfSource(source, document_iri="https://example.test/" + "x" * 100000)
    bounded = check_rdf_stream(huge, _vocabulary(), limits=Limits(max_bytes=1000))
    assert bounded.report.outcome == "exhausted" and bounded.document_iri is None
    assert all(len(i.location) < 1000 for i in bounded.report.issues)
    shared = Budget()
    shared.consume("issues", shared.remaining("issues"))
    assert check_rdf_stream(source, _vocabulary(), budget=shared).report.verified


def test_disk_vocabulary_uses_actual_sealed_targeted_declarations_and_scope(
    tmp_path: Path,
) -> None:
    schema = tmp_path / "schema.nq"
    schema.write_text(
        f"<urn:stream:p> <{RDF.type}> <{OWL.DatatypeProperty}> _:schema .\n"
        f"<urn:stream:C> <{RDF.type}> <{OWL.Class}> _:schema .\n"
    )
    limits = Limits(max_total_bytes=500000000, max_work=10000000, max_states=1000000)
    stored = snapshot_rdf(
        schema, tmp_path / "schema-snapshot", format="nquads", limits=limits
    )
    assert stored.report.verified and stored.snapshot is not None
    snapshot = stored.snapshot
    pin = snapshot.graph_names[0]
    assert isinstance(pin, BNode)
    source = tmp_path / "data.nt"
    source.write_text(
        f"<urn:stream:s> <{RDF.type}> <urn:stream:C> .\n"
        "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("named", pin), limits=limits
    )
    assert result.report.verified
    foreign = check_rdf_stream(
        source,
        snapshot,
        vocabulary_scope=QueryScope("named", BNode(str(pin))),
        limits=limits,
    )
    assert not foreign.report.verified and not any(
        i.kind == "finding" for i in foreign.report.issues
    )
    wrong_scope = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("default"), limits=limits
    )
    assert wrong_scope.report.outcome == "disagreement"
    assert not check_rdf_stream(source, snapshot, limits=limits).report.verified

    with source.open("a") as stream:
        stream.write(
            "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
            f"<urn:stream:s> <{RDF.type}> <urn:stream:MissingClass> .\n"
            f'<urn:stream:s> <urn:stream:p> "1.5"^^<{INTEGER}> .\n'
        )
    disk = check_rdf_stream(
        source, snapshot, vocabulary_scope=QueryScope("named", pin), limits=limits
    )
    memory = check_rdf_stream(source, _vocabulary(), limits=limits)
    assert disk.parse_report.verified and disk.statements == 5
    assert [
        (f.statement.index, f.issue.code, f.issue.actual) for f in disk.findings
    ] == [(f.statement.index, f.issue.code, f.issue.actual) for f in memory.findings]
    assert [f.statement.index for f in disk.findings] == [3, 4, 5]


def test_explicit_excluded_predicates_remain_in_checked_scope(tmp_path: Path) -> None:
    """A consumer can inspect the exact terms omitted from a passing local check."""
    source = tmp_path / "excluded.nt"
    source.write_text(
        "<urn:stream:s> <urn:stream:p> <urn:stream:o> .\n"
        "<urn:stream:s> <urn:stream:missing> <urn:stream:o> .\n"
    )
    result = check_rdf_stream(source, _vocabulary(), exclude=(str(E.missing),))
    assert result.report.verified and result.statements == 2
    assert str(E.missing) in result.local_report.exclusions
    assert {"shapes", "identity", "cardinality"} <= set(result.local_report.exclusions)
    assert not check_rdf_stream(source, _vocabulary()).report.verified
