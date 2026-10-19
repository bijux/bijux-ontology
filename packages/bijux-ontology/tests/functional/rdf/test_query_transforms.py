"""Real native transformations, explicit physical scope and atomic checked export."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import rdflib
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from bijux_ontology.declarations.path_fields import PathFieldDeclaration
from bijux_ontology.declarations.types import ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.export import write_export
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.models.path_fields import PathFieldUsage, check_path_fields
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate, MigrationCheck
from bijux_ontology.rdf.query_transforms import (
    SparqlBinding,
    SparqlProgram,
    transform_sparql,
)
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource

E = Namespace("urn:transform:")


def _source() -> PreparedSource:
    """Own exact original input independently from every returned candidate."""
    return prepare_rdf(
        Graph()
        .add((E.s, E.first, Literal("left")))
        .add((E.s, E.second, Literal("right")))
    )


def _joined(source: PreparedSource) -> Report:
    """Check the actual result term, rather than accepting program execution alone."""
    values = tuple(source.graph().objects(E.s, E.joined))
    paired = tuple(source.graph().objects(E.s, E.paired))
    return Report(
        "joined",
        1,
        ()
        if (
            len(values) == len(paired) == 1
            and str(values[0]) == "left"
            and str(paired[0]) == "right"
        )
        else (Issue("query_transform.join_missing", "Actual joined value is absent"),),
    )


def test_construct_join_and_exact_checked_atomic_export(tmp_path: Path) -> None:
    """Actual two-predicate joining passes selected checks and the exact export gate."""
    source = _source()
    original = frozenset(source.graph())
    program = SparqlProgram(
        "CONSTRUCT",
        "CONSTRUCT {?s <urn:transform:joined> ?a; <urn:transform:paired> ?b} "
        "WHERE {?s <urn:transform:first> ?a; <urn:transform:second> ?b}",
    )
    calls = []

    def actual(candidate: PreparedSource) -> Report:
        calls.append(frozenset(candidate.graph()))
        return _joined(candidate)

    result = transform_sparql(
        source, program, after_checks=(MigrationCheck("joined", actual),)
    )
    assert result.report.verified and result.source is not None
    assert len(calls) == 1 and len(result.source.graph()) == 2
    assert frozenset(source.graph()) == original
    exported = export_migration(result, accept=True, format="turtle")
    assert exported.report.verified and exported.content
    destination = tmp_path / "joined.ttl"
    destination.write_bytes(b"original destination")
    write_export(exported, destination, overwrite=True)
    roundtrip = prepare_rdf(RdfSource(destination, format="turtle"))
    assert _joined(roundtrip).verified
    incomplete = transform_sparql(
        prepare_rdf(Graph().add((E.s, E.first, Literal("left")))),
        program,
        after_checks=(MigrationCheck("joined", _joined),),
    )
    assert incomplete.report.outcome == "disagreement"
    missing = transform_sparql(source, program)
    assert missing.source is not None and not missing.report.verified
    blocked = export_migration(missing, accept=True, format="turtle")
    preserved = destination.read_bytes()
    with pytest.raises(InputError):
        write_export(blocked, destination, overwrite=True)
    assert destination.read_bytes() == preserved


def test_bound_update_preserves_exact_terms_and_destination_on_failure(
    tmp_path: Path,
) -> None:
    """Native parameters preserve lexical data and never become program syntax."""
    old = Literal("01", datatype=XSD.integer, normalize=False)
    source = prepare_rdf(Graph().add((E.s, E.value, old)))
    text = 'literal" } LOAD <http://example.invalid> #'
    supplied = Literal(text, lang="fr")
    binding = SparqlBinding("replacement", supplied)
    program = SparqlProgram(
        "UPDATE",
        "DELETE {?s <urn:transform:value> ?old} "
        "INSERT {?s <urn:transform:value> ?replacement} "
        "WHERE {?s <urn:transform:value> ?old}",
        (binding,),
    )
    observed = []

    def exact(candidate: PreparedSource) -> Report:
        value = candidate.graph().value(E.s, E.value)
        observed.append(value)
        return Report(
            "exact",
            1,
            ()
            if value == supplied
            else (
                Issue(
                    "query_transform.parameter_changed", "Exact RDF parameter changed"
                ),
            ),
        )

    before_flag = rdflib.NORMALIZE_LITERALS
    result = transform_sparql(
        source, program, after_checks=(MigrationCheck("exact", exact),)
    )
    assert result.report.verified and len(observed) == 1
    assert str(source.graph().value(E.s, E.value)) == "01"
    assert rdflib.NORMALIZE_LITERALS is before_flag
    assert isinstance(observed[0], Literal)
    assert observed[0].language == "fr" and str(observed[0]) == text
    integer = PathFieldDeclaration(
        "value", E.value, (ValueDeclaration("literal", datatype=str(XSD.integer)),)
    )

    def native(candidate: PreparedSource) -> Report:
        return check_path_fields(candidate, (PathFieldUsage(integer, E.s),))

    failed = transform_sparql(
        source, program, after_checks=(MigrationCheck("integer", native),)
    )
    assert failed.source is not None and failed.report.outcome == "disagreement"
    destination = tmp_path / "protected.ttl"
    destination.write_bytes(b"unchanged")
    with pytest.raises(InputError):
        write_export(
            export_migration(failed, accept=True, format="turtle"),
            destination,
            overwrite=True,
        )
    assert destination.read_bytes() == b"unchanged"
    lexical = transform_sparql(
        source,
        SparqlProgram(
            "UPDATE",
            "INSERT DATA {<urn:transform:s> <urn:transform:value> "
            '"1"^^<http://www.w3.org/2001/XMLSchema#integer>}',
        ),
        after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
    )
    assert lexical.report.verified and lexical.source is not None
    assert {str(value) for value in lexical.source.graph().objects(E.s, E.value)} == {
        "01",
        "1",
    }
    deleted = transform_sparql(
        lexical.source,
        SparqlProgram(
            "UPDATE",
            "DELETE DATA {<urn:transform:s> <urn:transform:value> "
            '"01"^^<http://www.w3.org/2001/XMLSchema#integer>}',
        ),
        after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
    )
    assert deleted.report.verified and deleted.source is not None
    assert [str(value) for value in deleted.source.graph().objects(E.s, E.value)] == [
        "1"
    ]
    invalid = SparqlProgram(
        "CONSTRUCT",
        "CONSTRUCT {?literal <urn:p> <urn:o>} "
        'WHERE {BIND("invalid RDF subject"^^<urn:datatype> AS ?literal)}',
    )
    invalid_result = transform_sparql(
        source, invalid, after_checks=(MigrationCheck("integer", native),)
    )
    assert invalid_result.source is None and not invalid_result.report.verified
    assert destination.read_bytes() == b"unchanged"


def test_named_empty_graph_and_owned_blank_lineage() -> None:
    """Physical graph identity survives empty results, original blanks and new nodes."""
    dataset = Dataset()
    dataset.graph(E.G).add((E.s, E.value, Literal("kept")))
    dataset.graph(E.hidden).add((E.s, E.secret, Literal("outside")))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(E.G)))

    def empty(candidate: PreparedSource) -> Report:
        assert candidate.graph_name == str(E.G)
        assert E.G in {graph.identifier for graph in candidate.dataset().graphs()}
        assert E.hidden not in {
            graph.identifier for graph in candidate.dataset().graphs()
        }
        return Report(
            "empty",
            1,
            ()
            if not candidate.graph()
            else (Issue("query_transform.not_empty", "Selected result is not empty"),),
        )

    result = transform_sparql(
        source,
        SparqlProgram("CONSTRUCT", "CONSTRUCT {?s ?p ?o} WHERE {?s <urn:absent> ?o}"),
        after_checks=(MigrationCheck("empty", empty),),
    )
    assert result.report.verified and result.source is not None
    assert len(source.graph()) == 1 and not result.source.graph()
    assert export_migration(result, accept=True, format="trig").report.verified
    unselected = transform_sparql(
        prepare_rdf(dataset), SparqlProgram("UPDATE", "DELETE WHERE {?s ?p ?o}")
    )
    assert unselected.source is None and not unselected.report.verified
    node = BNode()
    original = (
        Graph()
        .add((E.s, E.child, node))
        .add((node, E.value, Literal("a")))
        .add((node, E.value, Literal("b")))
    )
    owned = prepare_rdf(original)
    actual = owned.graph().value(E.s, E.child)
    assert isinstance(actual, BNode)
    program = SparqlProgram(
        "CONSTRUCT",
        "CONSTRUCT {<urn:transform:s> <urn:transform:child> ?child. "
        "?child <urn:transform:value> ?v. _:fresh <urn:transform:ref> ?child; "
        "<urn:transform:cell> ?v} WHERE {"
        "<urn:transform:s> <urn:transform:child> ?child. "
        "?child <urn:transform:value> ?v}",
        (SparqlBinding("child", actual),),
    )

    def lineage(candidate: PreparedSource) -> Report:
        graph = candidate.graph()
        child = graph.value(E.s, E.child)
        fresh = set(graph.subjects(E.ref, child))
        assert isinstance(child, BNode) and len(fresh) == 2
        assert child not in fresh and len(set(graph.objects(child, E.value))) == 2
        assert {str(graph.value(item, E.cell)) for item in fresh} == {"a", "b"}
        return Report("lineage", 1)

    assert transform_sparql(
        owned, program, after_checks=(MigrationCheck("lineage", lineage),)
    ).report.verified
    foreign = SparqlProgram(
        "UPDATE",
        "INSERT {?child <urn:extra> <urn:o>} WHERE {}",
        (SparqlBinding("child", BNode(str(actual))),),
    )
    assert transform_sparql(owned, foreign).source is None


def test_external_retrieval_rejected_from_actual_syntax_before_contact() -> None:
    """Nested/SILENT retrieval and update management cannot hide behind dead paths."""
    contacts = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Record actual contact, which every prohibited program must avoid."""
            contacts.append(self.path)
            self.send_error(503)

        def log_message(self, format: str, *args: object) -> None:
            """Keep this fixture's request evidence in its owned list only."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/external"
    try:
        programs = [
            SparqlProgram(
                "CONSTRUCT", f"CONSTRUCT {{?s ?p ?o}} FROM <{url}> WHERE {{?s ?p ?o}}"
            ),
            SparqlProgram(
                "CONSTRUCT",
                "CONSTRUCT {?s ?p ?o} WHERE {FILTER(false) "
                f"{{SELECT * WHERE {{SERVICE SILENT <{url}> {{?s ?p ?o}}}}}}}}",
            ),
            SparqlProgram("UPDATE", f"LOAD <{url}>"),
            SparqlProgram(
                "UPDATE", f"INSERT {{?s ?p ?o}} USING <{url}> WHERE {{?s ?p ?o}}"
            ),
            SparqlProgram(
                "UPDATE", f"WITH <{url}> DELETE {{?s ?p ?o}} WHERE {{?s ?p ?o}}"
            ),
            SparqlProgram(
                "UPDATE", "INSERT DATA {GRAPH <urn:g> {<urn:s> <urn:p> <urn:o>}}"
            ),
            SparqlProgram("UPDATE", "INSERT DATA {<urn:s> <urn:p> <urn:o>}; CLEAR ALL"),
        ]
        for program in programs:
            actual = transform_sparql(_source(), program)
            assert actual.source is None and not actual.report.verified
        assert contacts == []
        for text in (
            "CONSTRUCT {?s <urn:new> ?x} WHERE {?s <urn:p> ?x. "
            "BIND(<urn:replacement> AS ?x)}",
            "CONSTRUCT {<urn:s> <urn:new> ?x} WHERE {"
            "BIND(<urn:first> AS ?x) BIND(<urn:second> AS ?x)}",
            "CONSTRUCT {?s <urn:count> ?count} WHERE {"
            "{SELECT ?s (COUNT(?v) AS ?count) WHERE {?s ?p ?v}}}",
            "CONSTRUCT {?s <urn:new> ?x} WHERE {VALUES (?s) {(<urn:a> <urn:b>)}}",
            "CONSTRUCT {?s <urn:new> ?x} WHERE {VALUES (?s ?x) {(<urn:a>)}}",
            "CONSTRUCT {?s <urn:new> ?x} WHERE {"
            "_:same <urn:p> ?s. OPTIONAL {_:same <urn:q> ?x}}",
            "CONSTRUCT {?s <urn:new> ?x} WHERE {?s <urn:p> ?x} GROUP BY ?s",
        ):
            unsupported = transform_sparql(
                _source(),
                SparqlProgram("CONSTRUCT", text),
                after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
            )
            assert unsupported.source is None
            assert unsupported.report.outcome == "unverified"
        for text in (
            "DELETE DATA {?subject <urn:p> <urn:o>}",
            "INSERT DATA {?subject <urn:p> <urn:o>}",
        ):
            invalid = transform_sparql(
                _source(),
                SparqlProgram("UPDATE", text),
                after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
            )
            assert invalid.source is None and invalid.report.outcome == "input_error"
        plain = prepare_rdf(Graph().add((E.s, E.value, Literal("same"))))
        ambiguous = transform_sparql(
            plain,
            SparqlProgram(
                "UPDATE", 'DELETE DATA {<urn:transform:s> <urn:transform:value> "same"}'
            ),
            after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
        )
        assert ambiguous.source is None and not ambiguous.report.verified
        deleted = transform_sparql(
            plain,
            SparqlProgram(
                "UPDATE",
                "DELETE DATA {<urn:transform:s> <urn:transform:value> "
                '"same"^^<http://www.w3.org/2001/XMLSchema#string>}',
            ),
            after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
        )
        assert deleted.report.verified and deleted.source is not None
        assert not deleted.source.graph()
        safe = transform_sparql(
            _source(),
            SparqlProgram(
                "UPDATE",
                'INSERT DATA {<urn:s> <urn:p> "LOAD SERVICE FROM GRAPH"^^<http://www.w3.org/2001/XMLSchema#string>}',
            ),
            after_checks=(
                MigrationCheck(
                    "read-data",
                    lambda value: Report(
                        "data",
                        1,
                        ()
                        if (
                            URIRef("urn:s"),
                            URIRef("urn:p"),
                            Literal("LOAD SERVICE FROM GRAPH", datatype=XSD.string),
                        )
                        in value.graph()
                        else (
                            Issue(
                                "query_transform.data_missing",
                                "Actual literal data disappeared",
                            ),
                        ),
                    ),
                ),
            ),
        )
        assert safe.report.verified and contacts == []
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_shared_limits_cancellation_and_paid_native_postcondition_prefix() -> None:
    """Native findings survive late report adoption; missing coverage stays required."""
    source = prepare_rdf(Graph().add((E.s, E.value, Literal(1))))
    program = SparqlProgram(
        "UPDATE",
        "DELETE {?s <urn:transform:value> ?v} "
        "INSERT {?s <urn:transform:value> "
        '"bad"^^<http://www.w3.org/2001/XMLSchema#string>} '
        "WHERE {?s <urn:transform:value> ?v}",
    )
    field = PathFieldDeclaration(
        "value", E.value, (ValueDeclaration("literal", datatype=str(XSD.integer)),)
    )
    ends = []

    def run(cap: int) -> tuple[MigrationCandidate, int]:
        operation = Budget(Limits(max_work=cap))

        def actual(candidate: PreparedSource) -> Report:
            result = check_path_fields(
                candidate, (PathFieldUsage(field, E.s),), budget=operation
            )
            ends.append(cap - operation.remaining("work"))
            return result

        result = transform_sparql(
            source,
            program,
            after_checks=(MigrationCheck("actual", actual),),
            budget=operation,
        )
        paid = [
            issue
            for issue in operation._retained_issues.values()
            if issue.code == "emission.value_mismatch"
        ]
        assert all(
            any(retained is issue for retained in result.report.issues)
            for issue in paid
        )
        return result, len(paid)

    complete, paid = run(100000)
    assert complete.report.outcome == "disagreement" and paid
    end = ends[-1]
    rows = [run(cap) for cap in range(end, end + 24, 4)]
    assert any(value.report.outcome == "exhausted" and paid for value, paid in rows)
    for limit in (Limits(max_work=1), Limits(max_states=1), Limits(max_bytes=100)):
        result = transform_sparql(source, program, limits=limit)
        assert result.report.outcome == "exhausted" and result.source is None
    event = threading.Event()
    event.set()
    assert transform_sparql(source, program, cancel=event).report.outcome == "exhausted"
    owned = Budget(Limits(max_work=100000))
    assert transform_sparql(
        source,
        SparqlProgram("UPDATE", "INSERT DATA {<urn:s> <urn:p> <urn:o>}"),
        after_checks=(MigrationCheck("actual", lambda _: Report("actual", 1)),),
        budget=owned,
    ).report.verified
    owned.consume("work", owned.remaining("work"))
    assert transform_sparql(source, program, budget=owned).report.outcome == "exhausted"
