"""Actual materialization qualified by full source and emitted populations."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from rdflib import BNode, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.data.foreign_references.checking import check_foreign_references
from bijux_ontology.data.foreign_references.preparation import prepare_foreign_targets
from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceContract,
    ForeignReferenceResult,
)
from bijux_ontology.data.keys.extraction import _KeyExtraction
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyContract
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.export import export_rdf
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.emission.rdf import check_rdf_emission
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.path_traversal import finite_path_steps
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .preparation import _call, _identifier, _Map, inspect_mapping
from .types import (
    MorphBackend,
    R2rmlCheck,
    R2rmlForeignCheck,
    R2rmlPopulation,
    R2rmlRow,
    R2rmlTable,
    SqliteSnapshot,
)


def _capture_populations(
    populations: Sequence[R2rmlPopulation],
    operation: Budget,
) -> tuple[R2rmlPopulation, ...]:
    """Own every explicit per-map key/subject obligation before native work."""
    result = []
    seen = set()
    for population in capture_items(populations, R2rmlPopulation, operation):
        if type(population) is not R2rmlPopulation:
            raise InputError("Supply ordinary typed mapping populations")
        bound_node(population.triples_map, operation)
        if population.triples_map in seen:
            raise InputError("Each triples map needs one unique population")
        operation.consume("states", 2)
        seen.add(population.triples_map)
        _identifier(population.key_column, operation)
        key = population.emitted_key
        if type(key) is not KeyContract:
            raise InputError(
                "Population emitted keys require ordinary KeyContract records"
            )
        if (
            len(key.components) != 1
            or key.absence != "reject"
            or key.multiplicity != "reject"
            or key.components[0].equality != "rdf-term"
            or key.components[0].normalization != "none"
        ):
            raise ResourceError(
                "Population keys require one exact non-excluded emitted key"
            )
        rows = capture_items(population.rows, R2rmlRow, operation)
        if not rows:
            raise ResourceError("A required map population cannot be empty")
        subjects = set()
        for row in rows:
            if (
                type(row) is not R2rmlRow
                or type(row.usage) is not RdfModelUsage
                or type(row.key) is not Literal
                or type(row.usage.subject) is not URIRef
            ):
                raise InputError(
                    "Population rows require ordinary literal keys "
                    "and named model bindings"
                )
            bound_node(row.key, operation)
            bound_node(row.usage.subject, operation)
            absolute_iri(row.usage.subject)
            bound_text(row.usage.model, operation)
            bound_text(row.usage.location, operation)
            if row.key.language or row.key.datatype not in (
                None,
                XSD.string,
                XSD.integer,
            ):
                raise ResourceError(
                    "Source keys require integer or ordinary string literals"
                )
            if row.usage.subject in subjects:
                raise ResourceError(
                    "Source rows require unique expected subjects within each map"
                )
            operation.consume("states")
            subjects.add(row.usage.subject)
        operation.consume("bytes", 128)
        result.append(
            R2rmlPopulation(population.triples_map, population.key_column, key, rows)
        )
    return tuple(result)


def _row_key(
    row: R2rmlRow, operation: Budget, issues: IssueCollector
) -> tuple[type[object], object]:
    """Use maintained literal interpretation only for a declared source-key scalar."""
    checked = check_literal(row.key, budget=operation)
    retain_report(checked, operation, issues)
    if not checked.verified:
        raise InputError("A source key is not a valid supported literal")
    value = row.key.toPython()
    if type(value) not in (int, str):
        raise ResourceError("Source key literal has no qualified integer/string value")
    return type(value), value


def _source_table(
    connection: sqlite3.Connection,
    mapping: _Map,
    population: R2rmlPopulation,
    snapshot: SqliteSnapshot,
    operation: Budget,
    issues: IssueCollector,
) -> R2rmlTable:
    """Prove one full actual SQL key inventory without generating any RDF terms."""
    start = len(issues)
    # All raw selected SQLite scalar storage is bounded by the captured image.
    # This pays C-extension-to-Python value allocation before its first fetch.
    operation.consume("bytes", 8 * len(snapshot.content) + 2048)
    operation.consume("states", 4)
    tables = connection.execute(f'PRAGMA table_list("{mapping.table}")').fetchall()
    if len(tables) != 1 or tables[0][0:3] != ("main", mapping.table, "table"):
        raise ResourceError("Mappings require an existing ordinary main SQLite table")
    schema = connection.execute(f'PRAGMA table_xinfo("{mapping.table}")').fetchall()
    types = {}
    for field in schema:
        operation.consume("work")
        bound_text(field[1], operation)
        bound_text(field[2], operation)
        if field[6] != 0:
            raise ResourceError(
                "Generated/hidden SQL columns are outside this mapping profile"
            )
        types[field[1]] = field[2].upper()
    columns = {population.key_column, *(column.name for column in mapping.columns)}
    for name in columns:
        if types.get(name) not in ("INTEGER", "TEXT"):
            raise ResourceError(
                "A mapped SQLite column/type is unavailable or unsupported"
            )
    count = connection.execute(f'SELECT COUNT(*) FROM "{mapping.table}"').fetchone()[0]
    operation.consume("work")
    operation.consume("states", 2 * count + len(columns))
    operation.consume("bytes", count * (128 + 64 * len(columns)))
    names = tuple(sorted(columns))
    sql = (
        "SELECT "
        + ",".join('"' + name + '"' for name in names)
        + ' FROM "'
        + mapping.table
        + '"'
    )
    actual = set()
    for raw in connection.execute(sql):
        operation.consume("work", len(names) + 1)
        values = dict(zip(names, raw, strict=True))
        for name, value in values.items():
            if value is not None and type(value) not in (str, int):
                raise ResourceError(
                    "SQLite value is outside the qualified scalar profile"
                )
            if isinstance(value, str):
                bound_text(value, operation)
                if value == "":
                    raise ResourceError(
                        "Native empty-text normalization is outside the exact profile"
                    )
            if value is not None and (type(value) is int) != (types[name] == "INTEGER"):
                raise ResourceError(
                    "Dynamic SQLite scalar storage is outside the exact profile"
                )
        for column in mapping.columns:
            value = values[column.name]
            if column.role != "object" and value is None:
                raise ResourceError(
                    "Subject/join references require non-NULL actual values"
                )
            if column.role == "object":
                if types[column.name] == "INTEGER" and (
                    value is None or column.datatype != XSD.integer
                ):
                    raise ResourceError(
                        "Integer object columns require non-NULL values "
                        "and explicit integer datatype"
                    )
                if types[column.name] == "TEXT" and column.datatype not in (
                    None,
                    XSD.string,
                ):
                    raise ResourceError(
                        "Text numeric overrides are outside exact native interpretation"
                    )
        value = values[population.key_column]
        if value is None or (type(value), value) in actual:
            raise ResourceError("Actual source population keys are NULL or nonunique")
        actual.add((type(value), value))
    expected = set()
    for row in population.rows:
        operation.consume("work")
        value = _row_key(row, operation, issues)
        if value in expected:
            raise InputError("Caller source key inventory contains duplicate values")
        expected.add(value)
    if actual != expected:
        issues.append(
            Issue(
                "r2rml.source_population_unverified",
                "Actual SQL rows differ from the complete required key inventory",
                str(mapping.node),
                kind="unverified",
            )
        )
    report = Report(
        "r2rml.source_population",
        1,
        tuple(issues[start:]),
        requested=1,
        regime="actual-sqlite-key-inventory",
    )
    return R2rmlTable(
        mapping.node, mapping.logical, mapping.table, count, mapping.joins, report
    )


def _records(
    output: PreparedSource,
    population: R2rmlPopulation,
    graph_name: GraphSelection,
    operation: Budget,
) -> tuple[KeyRecord, ...]:
    """Bound source handles before constructors can parse their long identifiers."""
    operation.consume("states", len(population.rows))
    operation.consume("bytes", 64 + 128 * len(population.rows))
    result = []
    for index, row in enumerate(population.rows):
        operation.consume("work")
        bound_node(row.usage.subject, operation)
        identity = str(index)
        bound_text(identity, operation)
        result.append(
            KeyRecord(identity, output, row.usage.subject, graph_name=graph_name)
        )
    return tuple(result)


def _emitted_population(
    output: PreparedSource,
    population: R2rmlPopulation,
    graph_name: GraphSelection,
    operation: Budget,
    issues: IssueCollector,
) -> Report:
    """Use actual owned emitted paths to correlate every required source key."""
    start = len(issues)
    operation.consume("states", len(population.rows) + 1)
    operation.consume("bytes", 128 + 128 * len(population.rows))
    records = _records(output, population, graph_name, operation)
    extraction = _KeyExtraction(
        records, population.emitted_key, operation, issues, len(records)
    )
    seen = set()
    by_subject = {row.usage.subject: row for row in population.rows}
    for key in extraction:
        operation.consume("work")
        seen.add(key.record.subject)
        row = by_subject[key.record.subject]
        if len(key.values) != 1 or term_key(key.values[0]) != term_key(row.key):
            issues.append(
                Issue(
                    "r2rml.emitted_key_mismatch",
                    "The actual emitted subject does not retain "
                    "this source row's required key",
                    row.usage.location,
                )
            )
    if len(seen) != len(records):
        issues.append(
            Issue(
                "r2rml.emitted_population_unverified",
                "Not every actual source row has a qualified "
                "expected emitted subject/key",
                str(population.triples_map),
                kind="unverified",
            )
        )
    return Report(
        "r2rml.emitted_population",
        len(seen),
        tuple(issues[start:]),
        requested=len(records),
        complete=len(seen) == len(records),
        regime="source-row-and-emitted-subject-correspondence",
    )


def _foreign_coverage(
    mappings: tuple[_Map, ...],
    populations: tuple[R2rmlPopulation, ...],
    requests: tuple[R2rmlForeignCheck, ...],
    operation: Budget,
) -> tuple[tuple[R2rmlForeignCheck, R2rmlPopulation], ...]:
    """Require actual joined-endpoint paths over every child population."""
    expected = {}
    for mapping in mappings:
        for join in mapping.joins:
            operation.consume("states")
            operation.consume("bytes", 64)
            if join.object_map in expected:
                raise ResourceError(
                    "Shared referencing object-map ownership is unqualified"
                )
            expected[join.object_map] = (mapping, join)
    if len(requests) != len(expected):
        raise ResourceError(
            "Every selected parent join requires explicit foreign-reference coverage"
        )
    result = []
    seen = set()
    by_map = {population.triples_map: population for population in populations}
    for request in requests:
        if type(request) is not R2rmlForeignCheck:
            raise InputError("Supply ordinary typed foreign-reference requests")
        bound_node(request.object_map, operation)
        if request.object_map not in expected or request.object_map in seen:
            raise InputError(
                "Foreign checks require unique actual referencing object-map identities"
            )
        seen.add(request.object_map)
        mapping, join = expected[request.object_map]
        contract = request.contract
        if (
            type(request) is not R2rmlForeignCheck
            or type(contract) is not ForeignReferenceContract
        ):
            raise InputError("Supply ordinary typed foreign-reference contracts")
        if (
            contract.lookup != "closed"
            or contract.minimum_targets != 1
            or contract.maximum_targets != 1
            or contract.source.absence != "reject"
        ):
            raise ResourceError(
                "Required joins need closed non-excluded foreign target coverage"
            )
        relevant = False
        for component in contract.source.components:
            steps = finite_path_steps(
                capture_path(component.path, budget=operation), operation
            )
            relevant |= bool(steps and steps[0] == (join.predicate, False))
        if not relevant:
            raise ResourceError(
                "Foreign coverage must read the actual emitted joined endpoint"
            )
        if join.parent_column != by_map[join.parent_map].key_column:
            raise ResourceError(
                "This join profile requires the parent's unique declared population key"
            )
        targets = capture_items(request.targets, KeyRecord, operation)
        if type(request.target_complete) is not bool:
            raise InputError("Foreign corpus completeness requires a boolean")
        operation.consume("states")
        result.append(
            (
                R2rmlForeignCheck(
                    request.object_map, contract, targets, request.target_complete
                ),
                by_map[mapping.node],
            )
        )
    return tuple(result)


def check_r2rml(
    backend: MorphBackend,
    snapshot: SqliteSnapshot,
    mapping: PreparedSource,
    *,
    populations: Sequence[R2rmlPopulation],
    models: Sequence[ModelDeclaration],
    foreign_checks: Sequence[R2rmlForeignCheck] = (),
    graph_name: GraphSelection = "@default",
    mapping_graph: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> R2rmlCheck:
    """Materialize the real engine and verify complete explicit row/output contracts.

    The finite profile uses one local immutable SQLite snapshot, ordinary table
    mappings, one-column IRI templates, literal column objects, integer/string
    types and constant physical graph selection. Source keys and expected
    subjects remain independent obligations. Every parent join must be checked
    against an explicit independent corpus using its actual emitted link.
    No URI template, mapping engine or SQL view/function is reimplemented.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    tables: list[R2rmlTable] = []
    foreign_results: list[ForeignReferenceResult] = []
    output = None
    nquads = None
    model_report = None
    native_done = finished = False
    owned_snapshot = None
    owned_mapping = None
    try:
        operation.consume("states", 16)
        operation.consume("bytes", 2048)
        selected = _capture_populations(populations, operation)
        declarations = capture_items(models, ModelDeclaration, operation)
        requests = capture_items(foreign_checks, R2rmlForeignCheck, operation)
        if type(backend) is not MorphBackend or backend.version != "2.10.0":
            raise InputError("Supply an actually prepared MorphBackend")
        if type(snapshot) is not SqliteSnapshot or type(snapshot.content) is not bytes:
            raise InputError("Supply an actually prepared SQLite snapshot")
        if len(snapshot.content) > operation.limits.max_bytes:
            raise LimitExceeded(
                "SQLite snapshot exceeds this operation's byte allowance"
            )
        operation.consume("work", len(snapshot.content) + 1)
        operation.consume("bytes", len(snapshot.content))
        for text in (snapshot.sha256, snapshot.source_sha256, snapshot.source_name):
            bound_text(text, operation)
        if hashlib.sha256(snapshot.content).hexdigest() != snapshot.sha256:
            raise InputError("SQLite bytes differ from the owned snapshot hash")
        owned_snapshot = snapshot
        if not isinstance(mapping, PreparedSource):
            raise InputError("Mapping input requires an owned PreparedSource")
        if mapping.union:
            raise ResourceError("Mapping input requires one physical RDF graph")
        owned_mapping = mapping
        graph = mapping._read(mapping_graph, budget=operation)
        mappings = inspect_mapping(graph, operation)
        by_map = {population.triples_map: population for population in selected}
        if set(by_map) != {item.node for item in mappings}:
            raise ResourceError(
                "Every actual triples map needs its full required population"
            )
        if isinstance(graph_name, BNode):
            raise ResourceError(
                "This output profile requires a named IRI/default graph"
            )
        if type(graph_name) not in (str, URIRef):
            raise InputError("Select an ordinary default or named IRI output graph")
        bound_text(graph_name, operation)
        if graph_name != "@default":
            absolute_iri(graph_name)
        selected_graph = (
            "@default" if graph_name == "@default" else URIRef(str(graph_name))
        )
        if any(item.graph != selected_graph for item in mappings):
            raise ResourceError(
                "Output contracts must retain each mapping's exact physical graph"
            )
        owned_blanks = set()
        for triple in graph:
            operation.consume("work")
            for node in triple:
                if isinstance(node, BNode):
                    operation.consume("states")
                    operation.consume("bytes", 32)
                    owned_blanks.add(id(node))
        for identity in (
            *(population.triples_map for population in selected),
            *(request.object_map for request in requests),
        ):
            if isinstance(identity, BNode) and id(identity) not in owned_blanks:
                raise ResourceError(
                    "Blank mapping identity lacks original snapshot ownership"
                )
        covered_foreign = _foreign_coverage(mappings, selected, requests, operation)
        backend.artifacts.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="r2rml-", dir=backend.artifacts) as job:
            directory = Path(job)
            database = directory / "snapshot.sqlite"
            database.write_bytes(snapshot.content)
            stopped: list[LimitExceeded] = []
            with sqlite3.connect(
                database.as_uri() + "?mode=ro&immutable=1", uri=True
            ) as connection:

                def progress() -> int:
                    """Preserve the shared stop reason during actual SQLite work."""
                    try:
                        operation.consume("work", 100)
                    except LimitExceeded as error:
                        stopped.append(error)
                        return 1
                    return 0

                connection.set_progress_handler(progress, 100)
                try:
                    for item in mappings:
                        operation.consume("states", 2)
                        operation.consume("bytes", 128)
                        table = _source_table(
                            connection,
                            item,
                            by_map[item.node],
                            snapshot,
                            operation,
                            issues,
                        )
                        tables.append(table)
                        children.append(table.report)
                        if not table.report.verified:
                            raise ResourceError(
                                "Required source-table coverage is incomplete"
                            )
                except sqlite3.Error as error:
                    if stopped:
                        raise stopped[0] from error
                    raise ResourceError(
                        "SQLite source inspection/query did not complete"
                    ) from error
            exported = export_rdf(
                mapping, format="turtle", graph_name=mapping_graph, budget=operation
            )
            children.append(exported.report)
            retain_report(exported.report, operation, issues)
            if not exported.report.verified or exported.content is None:
                raise ResourceError("Original mapping could not be staged exactly")
            (directory / "mapping.ttl").write_bytes(exported.content)
            config = directory / "config.ini"
            content = "\n".join(
                (
                    "[CONFIGURATION]",
                    "output_format=N-QUADS",
                    "number_of_processes=1",
                    "logging_level=ERROR",
                    "na_values=",
                    "infer_sql_datatypes=no",
                    "output_file=" + str(directory / "output"),
                    "[DataSource1]",
                    "mappings=" + str(directory / "mapping.ttl"),
                    "db_url=sqlite:///"
                    + database.as_uri()
                    + "?mode=ro&immutable=1&uri=true",
                    "",
                )
            )
            bound_text(content, operation)
            config.write_text(content)
            cap = min(
                operation.limits.max_bytes, 2**31 - 1, operation.remaining("bytes") // 8
            )
            if cap < 512:
                raise LimitExceeded(
                    "Materialization has no remaining native output allowance"
                )
            operation.consume("bytes", cap + 1)
            _call(
                backend.python,
                {"action": "materialize", "config": str(config)},
                directory,
                operation,
                file_cap=cap,
            )
            native_done = True
            path = directory / "output.nq"
            if path.is_symlink() or not path.is_file():
                raise ResourceError(
                    "Actual materializer did not create its owned N-Quads artifact"
                )
            with path.open("rb") as reader:
                nquads = reader.read(cap + 1)
            if len(nquads) > cap:
                raise LimitExceeded("Actual native output exceeds its byte allowance")
            output = prepare_rdf(
                RdfSource(
                    nquads,
                    format="nquads",
                    document_iri="urn:bijux:ontology:r2rml:output",
                ),
                budget=operation,
            )
            count = sum(len(population.rows) for population in selected)
            operation.consume("states", count + 2)
            operation.consume("bytes", 256 + 16 * count)
            usages = tuple(
                row.usage for population in selected for row in population.rows
            )
            model_report = check_rdf_emission(
                output,
                declarations,
                usages,
                graph_name=selected_graph,
                budget=operation,
            )
            children.append(model_report)
            retain_report(model_report, operation, issues)
            for population in selected:
                operation.consume("states")
                operation.consume("bytes", 128)
                checked = _emitted_population(
                    output, population, selected_graph, operation, issues
                )
                children.append(checked)
            for request, population in covered_foreign:
                operation.consume("states", 4 + len(population.rows))
                operation.consume("bytes", 512 + 64 * len(population.rows))
                targets = prepare_foreign_targets(
                    request.targets,
                    request.contract,
                    complete=request.target_complete,
                    budget=operation,
                )
                children.append(targets.report)
                retain_report(targets.report, operation, issues)
                source_records = _records(output, population, selected_graph, operation)
                checked_foreign = check_foreign_references(
                    source_records,
                    targets,
                    source_complete=True,
                    models=declarations,
                    budget=operation,
                )
                foreign_results.append(checked_foreign)
                children.append(checked_foreign.report)
                retain_report(checked_foreign.report, operation, issues)
            finished = True
    except LimitExceeded as error:
        for report in children:
            issues.retain_completed(report)
        issues.exhausted(str(error), location="r2rml")
    except (InputError, OSError) as error:
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "r2rml.unverified" if unknown else "r2rml.input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="r2rml")
    pending = tuple(issue for issue in issues if issue.kind != "finding")
    native_report = Report(
        "r2rml.native",
        int(native_done),
        () if native_done else pending,
        requested=1,
        complete=native_done,
        regime="morph-kgc-2.10.0-isolated-cli",
    )
    children.append(native_report)
    compared = finished and all(child.complete for child in children)
    report = Report(
        "r2rml.mapping_output",
        int(compared),
        tuple(issues),
        requested=1,
        complete=compared,
        children=tuple(children),
        regime="explicit-source-rows-and-actual-materialized-rdf",
    )
    return R2rmlCheck(
        owned_snapshot,
        owned_mapping,
        output,
        nquads,
        tuple(tables),
        tuple(foreign_results),
        model_report,
        native_report,
        report,
    )
