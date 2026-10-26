"""Actual read-only SQLite population qualification for one mapping plan."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.literals.checking import check_literal

from .plan import _Map, _MappingPlan
from .types import (
    R2rmlPopulation,
    R2rmlRow,
    R2rmlTable,
    SqliteSnapshot,
)


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


def qualify_source(
    plan: _MappingPlan,
    directory: Path,
    operation: Operation,
    issues: IssueCollector,
    tables: list[R2rmlTable],
    children: list[Report],
) -> None:
    """Retain each completed source proof before inspecting another table."""
    database = directory / "snapshot.sqlite"
    database.write_bytes(plan.snapshot.content)
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
            for item in plan.mappings:
                operation.consume("states", 2)
                operation.consume("bytes", 128)
                table = _source_table(
                    connection,
                    item,
                    plan.by_map[item.node],
                    plan.snapshot,
                    operation,
                    issues,
                )
                tables.append(table)
                children.append(table.report)
                if not table.report.verified:
                    raise ResourceError("Required source-table coverage is incomplete")
        except sqlite3.Error as error:
            if stopped:
                raise stopped[0] from error
            raise ResourceError(
                "SQLite source inspection/query did not complete"
            ) from error
