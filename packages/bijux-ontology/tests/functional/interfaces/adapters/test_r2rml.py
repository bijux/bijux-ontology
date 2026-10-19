"""Actual isolated Morph-KGC output retains complete source/relationship coverage."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.foreign_references import ForeignReferenceContract
from bijux_ontology.data.keys import KeyRecord
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    OutputRequirement,
    ValueDeclaration,
)
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.interfaces.adapters.r2rml import (
    MorphBackend,
    R2rmlCheck,
    R2rmlForeignCheck,
    R2rmlPopulation,
    R2rmlRow,
    SqliteSnapshot,
    check_r2rml,
    prepare_morph_backend,
    prepare_sqlite_snapshot,
)
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import term_key

ID = URIRef("urn:example:id")
PARENT = URIRef("urn:example:parent")
VALUE = URIRef("urn:example:value")
GRAPH = URIRef("urn:graph:data")
PARENT_MAP = URIRef("urn:mapping:parent")
CHILD_MAP = URIRef("urn:mapping:child")
JOIN_MAP = URIRef("urn:join:child-parent")
MAPPING = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix ex: <urn:example:> .
<urn:mapping:parent> a rr:TriplesMap;
 rr:logicalTable [rr:tableName "parent"];
 rr:subjectMap [rr:template "urn:parent:{id}";rr:class ex:Parent;
 rr:graph <urn:graph:data>];
 rr:predicateObjectMap [rr:predicate ex:id;
 rr:objectMap [rr:column "id";rr:datatype xsd:integer]];
 rr:predicateObjectMap [rr:predicate ex:label;rr:objectMap [rr:column "label"]].
<urn:mapping:child> a rr:TriplesMap;
 rr:logicalTable [rr:tableName "child"];
 rr:subjectMap [rr:template "urn:child:{id}";rr:class ex:Child;
 rr:graph <urn:graph:data>];
 rr:predicateObjectMap [rr:predicate ex:id;
 rr:objectMap [rr:column "id";rr:datatype xsd:integer]];
 rr:predicateObjectMap [rr:predicate ex:value;rr:objectMap [rr:column "value"]];
 rr:predicateObjectMap [rr:predicate ex:parent;rr:objectMap <urn:join:child-parent>].
<urn:join:child-parent> rr:parentTriplesMap <urn:mapping:parent>;
 rr:joinCondition [rr:child "parent_fk";rr:parent "id"].
"""


@dataclass(frozen=True)
class _Case:
    """Actual local database/mapping beside independent consumer expectations."""

    path: Path
    digest: str
    snapshot: SqliteSnapshot
    mapping: PreparedSource
    populations: tuple[R2rmlPopulation, ...]
    models: tuple[ModelDeclaration, ...]
    foreign: tuple[R2rmlForeignCheck, ...]


def _case(path: Path, mode: str = "valid") -> _Case:
    """Produce a real SQLite source, never replacement materializer output."""
    path.mkdir(parents=True, exist_ok=True)
    database = path / "source.sqlite"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            "CREATE TABLE parent(id INTEGER PRIMARY KEY,label TEXT NOT NULL); "
            "CREATE TABLE child(id INTEGER PRIMARY KEY,parent_fk INTEGER NOT NULL,"
            "wrong_fk INTEGER NOT NULL,value TEXT);"
        )
        connection.executemany(
            "INSERT INTO parent VALUES (?,?)", ((1, "Alpha"), (2, "Beta"))
        )
        connection.executemany(
            "INSERT INTO child VALUES (?,?,?,?)",
            ((10, 1, 2, "" if mode == "empty" else "003"), (11, 2, 1, None)),
        )
        if mode == "incomplete":
            connection.execute("DELETE FROM child WHERE id=11")
        if mode == "native_collision":
            connection.execute("ALTER TABLE child RENAME COLUMN parent_fk TO parent_id")
    mapping = MAPPING
    if mode == "predicate":
        mapping = mapping.replace("rr:predicate ex:id", "rr:predicate ex:wrongId")
    elif mode == "template":
        mapping = mapping.replace("urn:child:{id}", "urn:wrong:{id}")
    elif mode == "wrong_join":
        mapping = mapping.replace('rr:child "parent_fk"', 'rr:child "wrong_fk"')
    elif mode == "native_collision":
        mapping = mapping.replace('rr:child "parent_fk"', 'rr:child "parent_id"')
    elif mode == "extension":
        mapping += (
            "<urn:mapping:child> <https://w3id.org/rml/logicalSource> "
            '[<https://w3id.org/rml/source> "https://example.invalid/private"].'
        )
    elif mode == "sql_query":
        mapping = mapping.replace(
            'rr:tableName "child"', 'rr:sqlQuery "SELECT * FROM child"'
        )
    elif mode == "failed_query":
        mapping = mapping.replace('rr:column "value"', 'rr:column "missing_column"')
    elif mode == "orphan":
        mapping += "<urn:mapping:orphan> a rr:TriplesMap."
    elif mode == "numeric_override":
        mapping = mapping.replace(
            'rr:column "value"', 'rr:column "value";rr:datatype xsd:integer'
        )
    source = prepare_rdf(RdfSource(mapping, format="turtle"))
    key = KeyContract("emitted-id", (KeyComponent(ID),))
    populations = tuple(
        R2rmlPopulation(
            node,
            "id",
            key,
            tuple(
                R2rmlRow(
                    Literal(number),
                    RdfModelUsage(
                        model,
                        URIRef(prefix + str(number)),
                        location=prefix + str(number),
                    ),
                )
                for number in numbers
            ),
        )
        for node, model, prefix, numbers in (
            (PARENT_MAP, "Parent", "urn:parent:", (1, 2)),
            (CHILD_MAP, "Child", "urn:child:", (10, 11)),
        )
    )
    integer = ValueDeclaration("literal", datatype=str(XSD.integer))
    text = ValueDeclaration("literal", datatype=str(XSD.string))
    identifier = FieldDeclaration("id", str(ID), (integer,), emission="guaranteed")
    models = (
        ModelDeclaration(
            "Parent",
            ("urn:example:Parent",),
            (
                identifier,
                FieldDeclaration(
                    "label", "urn:example:label", (text,), emission="guaranteed"
                ),
            ),
        ),
        ModelDeclaration(
            "Child",
            ("urn:example:Child",),
            (
                identifier,
                FieldDeclaration("value", str(VALUE), (text,)),
                FieldDeclaration(
                    "parent",
                    str(PARENT),
                    (ValueDeclaration("iri", types=("urn:example:Parent",)),),
                    emission="guaranteed",
                    output=OutputRequirement(minimum_values=1, maximum_values=1),
                ),
            ),
        ),
    )
    expected = Graph()
    for number, parent in ((10, 1), (11, 2)):
        node = URIRef("urn:expected:" + str(number))
        expected.add((node, ID, Literal(number)))
        expected.add((node, PARENT, URIRef("urn:parent:" + str(parent))))
    corpus = prepare_rdf(expected)
    contract = ForeignReferenceContract(
        "child-parent",
        KeyContract("actual-link", (KeyComponent(ID), KeyComponent(PARENT))),
        KeyContract("expected-link", (KeyComponent(ID), KeyComponent(PARENT))),
    )
    foreign = (
        R2rmlForeignCheck(
            JOIN_MAP,
            contract,
            tuple(
                KeyRecord(str(number), corpus, URIRef("urn:expected:" + str(number)))
                for number in (10, 11)
            ),
            True,
        ),
    )
    digest = hashlib.sha256(database.read_bytes()).hexdigest()
    snapshot = prepare_sqlite_snapshot(database, artifacts=path / "capture")
    return _Case(database, digest, snapshot, source, populations, models, foreign)


@pytest.fixture
def backend(tmp_path: Path) -> MorphBackend:
    """Require an explicit actually installed separate native interpreter."""
    python = os.environ.get("BIJUX_MORPH_PYTHON")
    if not python:
        pytest.skip(
            "An explicit separate installed Morph-KGC backend is not configured"
        )
    return prepare_morph_backend(Path(python), artifacts=tmp_path / "jobs")


def _check(backend: MorphBackend, case: _Case) -> R2rmlCheck:
    """Use ordinary selected public population, emission and foreign contracts."""
    return check_r2rml(
        backend,
        case.snapshot,
        case.mapping,
        populations=case.populations,
        models=case.models,
        foreign_checks=case.foreign,
        graph_name=GRAPH,
    )


def test_native_named_graph_typed_values_null_and_complete_row_bindings(
    backend: MorphBackend, tmp_path: Path
) -> None:
    case = _case(tmp_path / "data")
    result = _check(backend, case)
    assert result.report.verified, result.report.to_dict()
    assert importlib.util.find_spec("morph_kgc") is None
    assert result.output is not None and result.nquads is not None
    graph = result.output.graph(GRAPH)
    assert len(graph) == 13
    assert not list(graph.objects(URIRef("urn:child:11"), VALUE))
    actual = tuple(graph.objects(URIRef("urn:child:10"), VALUE))
    assert len(actual) == 1 and term_key(actual[0]) == term_key(Literal("003"))
    assert b'"003"' in result.nquads
    assert (URIRef("urn:child:10"), ID, Literal(10)) in graph
    assert {table.rows for table in result.tables} == {2}
    assert all(table.report.verified for table in result.tables)
    assert result.foreign_results[0].report.verified
    assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.digest


def test_real_output_mutations_and_same_count_wrong_endpoint_cannot_pass(
    backend: MorphBackend, tmp_path: Path
) -> None:
    for mode in ("predicate", "template", "wrong_join"):
        case = _case(tmp_path / mode, mode)
        result = _check(backend, case)
        assert result.native_report.verified and result.output is not None
        assert not result.report.verified
        assert len(result.output.graph(GRAPH)) == 13
        if mode == "wrong_join":
            assert result.model_report is not None and result.model_report.verified
            assert (
                result.foreign_results and not result.foreign_results[0].report.verified
            )
            assert all(
                row.status == "missing" for row in result.foreign_results[0].resolutions
            )
        assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.digest


def test_missing_source_rows_maps_join_coverage_and_ambiguous_bindings_are_unknown(
    backend: MorphBackend, tmp_path: Path
) -> None:
    incomplete = _check(backend, _case(tmp_path / "incomplete", "incomplete"))
    assert incomplete.output is None and not incomplete.report.verified
    orphan = _check(backend, _case(tmp_path / "orphan", "orphan"))
    assert orphan.output is None and not orphan.native_report.verified
    assert orphan.report.outcome == "unverified"
    case = _case(tmp_path / "data")
    for populations, foreign in (
        (case.populations[:1], case.foreign),
        (case.populations, ()),
    ):
        result = check_r2rml(
            backend,
            case.snapshot,
            case.mapping,
            populations=populations,
            models=case.models,
            foreign_checks=foreign,
            graph_name=GRAPH,
        )
        assert result.output is None and not result.report.verified
    child = case.populations[1]
    duplicate = replace(
        child, rows=(child.rows[0], replace(child.rows[1], usage=child.rows[0].usage))
    )
    result = check_r2rml(
        backend,
        case.snapshot,
        case.mapping,
        populations=(case.populations[0], duplicate),
        models=case.models,
        foreign_checks=case.foreign,
        graph_name=GRAPH,
    )
    assert result.output is None and not result.report.verified


def test_extensions_queries_and_native_scalar_limitations_remain_unqualified(
    backend: MorphBackend, tmp_path: Path
) -> None:
    for mode in ("extension", "sql_query", "empty", "numeric_override"):
        case = _case(tmp_path / mode, mode)
        result = _check(backend, case)
        assert result.output is None and not result.native_report.verified
        assert result.report.outcome == "unverified", result.report.to_dict()
        assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.digest


def test_failed_source_and_native_join_never_adopt_partial_materialization(
    backend: MorphBackend, tmp_path: Path
) -> None:
    for mode in ("native_collision", "failed_query"):
        case = _case(tmp_path / mode, mode)
        result = _check(backend, case)
        assert result.report.outcome == "unverified", result.report.to_dict()
        assert result.output is None and result.nquads is None
        assert not result.native_report.verified
        assert hashlib.sha256(case.path.read_bytes()).hexdigest() == case.digest


def test_shared_cuts_cancel_and_fresh_actual_recovery(
    backend: MorphBackend, tmp_path: Path
) -> None:
    case = _case(tmp_path / "data")
    for limits in (
        Limits(max_work=1),
        Limits(max_states=1),
        Limits(max_total_bytes=128),
    ):
        stopped = check_r2rml(
            backend,
            case.snapshot,
            case.mapping,
            populations=case.populations,
            models=case.models,
            foreign_checks=case.foreign,
            graph_name=GRAPH,
            budget=Budget(limits),
        )
        assert stopped.report.outcome == "exhausted" and stopped.output is None
    event = Event()
    event.set()
    cancelled = check_r2rml(
        backend,
        case.snapshot,
        case.mapping,
        populations=case.populations,
        models=case.models,
        foreign_checks=case.foreign,
        graph_name=GRAPH,
        cancel=event,
    )
    assert cancelled.report.outcome == "exhausted" and cancelled.output is None
    assert _check(backend, case).report.verified
