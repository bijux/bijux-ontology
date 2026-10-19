"""Finite explicit CSVW mapping through maintained metadata and cell APIs."""

import io
import warnings
from collections.abc import Mapping
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from importlib.metadata import version
from typing import Any

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Location
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.json.values import owned_json
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import bound_node

from .types import CsvwCell

_INHERITED = frozenset(
    (
        "aboutUrl",
        "propertyUrl",
        "valueUrl",
        "datatype",
        "null",
        "default",
        "separator",
        "required",
    )
)
_DATATYPES = frozenset(("string", "integer", "decimal", "boolean", "anyURI"))


class UnsupportedCsvw(ResourceError):
    """Metadata outside the declared finite profile cannot establish extraction."""


def backend() -> tuple[Any, Any, Any]:
    """Import only the explicitly selected maintained optional CSVW capability."""
    try:
        from csvw import (  # type: ignore[import-untyped]
            Dialect,
            Table,
            UnicodeReaderWithLineNumber,
        )
    except ImportError as error:
        raise UnsupportedCsvw(
            "CSVW extraction requires bijux-ontology[csvw]"
        ) from error
    installed = version("csvw").split(".")
    if installed[0] != "4" or int(installed[1]) < 1:
        raise UnsupportedCsvw("CSVW extraction requires csvw>=4.1,<5")
    return Table, Dialect, UnicodeReaderWithLineNumber


def _keys(value: object, allowed: frozenset[str], name: str) -> dict[str, Any]:
    """Decline unknown rules before native metadata construction can fetch."""
    if type(value) is not dict:
        raise UnsupportedCsvw("CSVW " + name + " must be an inline object")
    if any(key not in allowed for key in value):
        raise UnsupportedCsvw("Unsupported CSVW " + name + " metadata")
    return value


def _inherited(value: dict[str, Any]) -> None:
    """Keep the supported native scalar, null and unordered-separator regime."""
    for key in ("aboutUrl", "propertyUrl", "valueUrl", "separator", "default"):
        if key in value and type(value[key]) is not str:
            raise UnsupportedCsvw("CSVW templates and scalar policies require text")
    if "separator" in value and not value["separator"]:
        raise UnsupportedCsvw("Empty CSVW separators are unsupported")
    if "required" in value and type(value["required"]) is not bool:
        raise InputError("CSVW required must be boolean")
    if "null" in value:
        null = value["null"]
        if type(null) is not str and not (
            type(null) is list and all(type(item) is str for item in null)
        ):
            raise UnsupportedCsvw("CSVW null requires text or a finite text array")
    if "datatype" in value and (
        type(value["datatype"]) is not str or value["datatype"] not in _DATATYPES
    ):
        raise UnsupportedCsvw("Unsupported CSVW datatype or datatype facets")


def metadata(value: object, base: str, budget: Budget) -> tuple[Any, Any, Any]:
    """Own and qualify every rule before invoking maintained metadata APIs."""
    bound_text(base, budget)
    absolute_iri(base)
    owned, _ = owned_json(value, budget)
    table = _keys(
        owned, _INHERITED | {"@context", "url", "tableSchema", "dialect"}, "table"
    )
    if table.get("@context") != "http://www.w3.org/ns/csvw":
        raise UnsupportedCsvw("CSVW extraction requires its standard context")
    if type(table.get("url")) is not str or not table["url"]:
        raise InputError("CSVW tables require an explicit resource URL")
    _inherited(table)
    if "null" in table or "default" in table:
        raise UnsupportedCsvw("Null and default policies require column declarations")
    schema = _keys(
        table.get("tableSchema"), _INHERITED | {"columns", "primaryKey"}, "tableSchema"
    )
    _inherited(schema)
    if "null" in schema or "default" in schema:
        raise UnsupportedCsvw("Null and default policies require column declarations")
    columns = schema.get("columns")
    if type(columns) is not list or not columns:
        raise UnsupportedCsvw("CSVW extraction requires explicit physical columns")
    names: set[str] = set()
    for column in columns:
        column = _keys(column, _INHERITED | {"name", "suppressOutput"}, "column")
        _inherited(column)
        name = column.get("name")
        if type(name) is not str or not name or name in names:
            raise InputError("CSVW columns require distinct explicit names")
        budget.consume("states")
        names.add(name)
        if "suppressOutput" in column and type(column["suppressOutput"]) is not bool:
            raise InputError("CSVW suppressOutput must be boolean")
    primary = schema.get("primaryKey", [])
    primary = [primary] if type(primary) is str else primary
    if type(primary) is not list or any(
        type(name) is not str or name not in names for name in primary
    ):
        raise InputError("CSVW primaryKey must select declared physical columns")
    dialect = _keys(
        table.get("dialect", {}),
        frozenset(("delimiter", "header", "quoteChar", "doubleQuote")),
        "dialect",
    )
    for key in ("header", "doubleQuote"):
        if key in dialect and type(dialect[key]) is not bool:
            raise InputError("CSVW dialect flags must be boolean")
    for key in ("delimiter", "quoteChar"):
        if key in dialect and (type(dialect[key]) is not str or len(dialect[key]) != 1):
            raise InputError("CSVW dialect delimiters require a single character")
    if "primaryKey" in schema and (not primary or len(set(primary)) != len(primary)):
        raise InputError("CSVW primaryKey requires distinct column references")
    native_table, native_dialect, reader = backend()
    table["@base"] = base
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try:
            actual = native_table.fromvalue(table)
            parsed_dialect = native_dialect(commentPrefix=None, **dialect)
        except Warning as error:
            raise UnsupportedCsvw(
                "Native CSVW metadata was not fully supported"
            ) from error
    budget.consume("states", len(columns))
    native_columns = {column.name: column for column in actual.tableSchema.columns}
    for name in primary:
        if native_columns[name].inherit("separator"):
            raise UnsupportedCsvw("CSVW primary keys require scalar columns")
    for column in actual.tableSchema.columns:
        if column.suppressOutput:
            continue
        if not column.inherit("aboutUrl") or not column.inherit("propertyUrl"):
            raise UnsupportedCsvw(
                "Every emitted cell requires explicit RDF URI mappings"
            )
        if column.inherit("separator") and column.inherit("valueUrl"):
            raise UnsupportedCsvw("List-valued CSVW valueUrl is unsupported")
        for key in ("aboutUrl", "propertyUrl", "valueUrl"):
            template = column.inherit(key)
            if template is not None:
                for variable in template.variable_names:
                    budget.consume("work")
                    if variable in native_columns and native_columns[variable].inherit(
                        "separator"
                    ):
                        raise UnsupportedCsvw(
                            "CSVW URI variables require scalar columns"
                        )
                    if variable not in names and variable not in ("_row", "_name"):
                        raise UnsupportedCsvw(
                            "CSVW URI templates reference unknown columns"
                        )
    return actual, parsed_dialect, reader


def resources(tables: Mapping[str, str], budget: Budget) -> dict[str, str]:
    """Capture exact supplied text resources without a fallback retrieval path."""
    if not isinstance(tables, Mapping):
        raise InputError("CSVW tables require an explicit URL-to-text mapping")
    budget.consume("states", len(tables))
    result: dict[str, str] = {}
    for url, text in tables.items():
        if type(url) is not str or type(text) is not str:
            raise InputError("CSVW resources require URL and CSV text strings")
        bound_text(url, budget)
        absolute_iri(url)
        bound_text(text, budget)
        budget.consume("resources")
        result[url] = text
    return result


def _iri(
    table: Any,
    template: Any,
    row: dict[str, Any],
    ordinal: int,
    name: str,
    budget: Budget,
    payload_size: int,
) -> URIRef:
    """Use maintained expansion after charging a conservative output allowance."""
    size = len(template.uri) + 1
    for variable in template.variable_names:
        budget.consume("work")
        if variable in row and (
            row[variable] is None or isinstance(row[variable], list)
        ):
            raise UnsupportedCsvw("CSVW URI variables require present scalar cells")
    allowance = 12 * size + (len(template.variables) + 1) * 12 * (
        payload_size + len(name) + 32
    )
    budget.consume("work", allowance)
    budget.consume("bytes", allowance)
    if allowance > budget.limits.max_bytes:
        raise LimitExceeded("CSVW URI expansion exceeds max_bytes")
    try:
        value = table.expand(template, row, _row=ordinal, _name=name)
    except (ValueError, ArithmeticError) as error:
        raise UnsupportedCsvw(
            "Native CSVW value lacks a qualified URI-template encoding"
        ) from error
    if type(value) is not str:
        raise UnsupportedCsvw("CSVW RDF templates require absolute URI outputs")
    bound_text(value, budget)
    return URIRef(absolute_iri(value))


def _object(column: Any, value: Any, budget: Budget, size: int) -> Literal:
    """Encode authoritative native scalar values without float conversion."""
    budget.consume("work", 12 * size + 128)
    budget.consume("bytes", 12 * size + 128)
    datatype = column.inherit("datatype")
    kind = datatype.base if datatype else "string"
    if kind == "anyURI":
        value = value.unsplit()
    try:
        result = Literal(value, datatype=URIRef(str(XSD) + kind), normalize=False)
    except (ValueError, ArithmeticError) as error:
        raise UnsupportedCsvw(
            "Native CSVW value lacks a qualified RDF datatype encoding"
        ) from error
    bound_node(result, budget)
    report = check_literal(result, budget=budget)
    if not report.verified:
        raise UnsupportedCsvw(
            "Native CSVW value lacks a qualified RDF datatype encoding"
        )
    return result


def _read(column: Any, value: str, budget: Budget) -> Any:
    """Keep native decimal interpretation independent of caller context settings."""
    default = column.inherit("default")
    effective = value or default
    separator = column.inherit("separator")
    budget.consume("work", len(effective) + 1)
    members = effective.count(separator) + 1 if separator else 1
    payload = 4 * len(effective)
    if separator and members > 1:
        # Native split parsing can replace every empty member with this default.
        payload += 4 * len(default) * members
    if payload > budget.limits.max_bytes:
        raise LimitExceeded("Expanded CSVW cell exceeds max_bytes")
    budget.consume("states", members)
    budget.consume("work", payload + members)
    budget.consume("bytes", payload + 128 * members)
    precision = len(value) + len(default) + 16
    context = Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=-precision,
        Emax=precision,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )
    with localcontext(context):
        return column.read(value)


def convert(
    table: Any,
    dialect: Any,
    reader_class: Any,
    text: str,
    budget: Budget,
    issues: IssueCollector,
    cells: list[CsvwCell],
) -> Graph:
    """Read actual rows and retain their explicit native cell-to-RDF mapping."""
    graph = Graph()
    columns = table.tableSchema.columns
    url = str(table.url.resolve(table.base))
    bound_text(url, budget)
    keys: set[tuple[object, ...]] = set()
    budget.consume("work", len(text) + 1)
    budget.consume("bytes", 8 * len(text) + 64)
    with reader_class(io.StringIO(text), dialect=dialect) as reader:
        last_line = 0
        if dialect.header:
            try:
                last_line, header = next(reader)
            except StopIteration as error:
                raise InputError("CSVW table has no declared header") from error
            if header != [column.name for column in columns]:
                raise UnsupportedCsvw(
                    "CSVW header must match declared physical columns"
                )
        ordinal = 0
        for end_line, raw in reader:
            ordinal += 1
            line, last_line = last_line + 1, end_line
            budget.consume("states", 2 * len(columns) + 1)
            budget.consume("bytes", 128 * len(columns) + 32)
            budget.consume("work", len(raw) + 1)
            if len(raw) != len(columns):
                raise InputError("CSVW row width differs from its explicit schema")
            payload_size = sum(len(value) for value in raw) + sum(
                len(column.inherit("default")) + 16 for column in columns
            )
            parsed: dict[str, Any] = {}
            valid = True
            for index, (column, value) in enumerate(zip(columns, raw, strict=True), 1):
                bound_text(value, budget)
                try:
                    parsed[column.name] = _read(column, value, budget)
                except LimitExceeded:
                    raise
                except (ValueError, ArithmeticError):
                    valid = False
                    issues.append(
                        Issue(
                            "csvw.cell_invalid",
                            "Cell violates its declared CSVW datatype or presence",
                            details=Location(
                                source=url, path=("rows", ordinal, "columns", index)
                            ),
                            actual=value,
                        )
                    )
            if valid and table.tableSchema.primaryKey:
                key = tuple(parsed[name] for name in table.tableSchema.primaryKey)
                if any(value is None or isinstance(value, list) for value in key):
                    raise UnsupportedCsvw(
                        "CSVW primary keys require present scalar values"
                    )
                if key in keys:
                    issues.append(
                        Issue(
                            "csvw.duplicate_key",
                            "Declared CSVW primary key repeats",
                            details=Location(source=url, path=("rows", ordinal)),
                        )
                    )
                else:
                    budget.consume("states")
                    keys.add(key)
            for index, (column, value) in enumerate(zip(columns, raw, strict=True), 1):
                emitted: list[tuple[URIRef, URIRef, URIRef | Literal]] = []
                item = parsed.get(column.name)
                if valid and not column.suppressOutput and item is not None:
                    subject = _iri(
                        table,
                        column.inherit("aboutUrl"),
                        parsed,
                        ordinal,
                        column.name,
                        budget,
                        payload_size,
                    )
                    predicate = _iri(
                        table,
                        column.inherit("propertyUrl"),
                        parsed,
                        ordinal,
                        column.name,
                        budget,
                        payload_size,
                    )
                    values = item if isinstance(item, list) else [item]
                    for member in values:
                        if member is None:
                            continue
                        target = column.inherit("valueUrl")
                        if target is not None and isinstance(item, list):
                            raise UnsupportedCsvw(
                                "List-valued CSVW valueUrl is unsupported"
                            )
                        obj = (
                            _iri(
                                table,
                                target,
                                parsed,
                                ordinal,
                                column.name,
                                budget,
                                payload_size,
                            )
                            if target
                            else _object(column, member, budget, payload_size)
                        )
                        budget.consume("triples")
                        budget.consume("states")
                        triple = subject, predicate, obj
                        graph.add(triple)
                        emitted.append(triple)
                cells.append(
                    CsvwCell(
                        url, ordinal, line, index, column.name, value, tuple(emitted)
                    )
                )
    return graph
