"""Bounded ordinary JSON and SPARQL files for portable local query obligations."""

import json
from pathlib import Path
from typing import Literal, cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.json.values import (
    decode_json,
    known_keys,
    object_value,
    own_json,
    text_value,
)
from bijux_ontology.rdf.queries.access import QueryScope
from bijux_ontology.rdf.queries.protocol import read_term

from .types import QueryAnswers, QueryRule


def _read(path: Path, budget: Budget) -> str:
    """Own one explicitly named local UTF-8 file under cumulative byte bounds."""
    budget.consume("resources")
    try:
        with path.open("rb") as stream:
            raw = stream.read(budget.limits.max_bytes + 1)
        if len(raw) > budget.limits.max_bytes:
            raise LimitExceeded("Query rule file exceeds max_bytes", location=str(path))
        budget.consume("bytes", len(raw))
        return raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ResourceError(f"Cannot read query rule file: {path}") from error


def _expected(value: object, budget: Budget) -> bool | QueryAnswers | None:
    """Decode exact RDF term fields without treating ordinary JSON values as RDF."""
    if value is None or type(value) is bool:
        return value
    data = object_value(value, location="expected")
    known_keys(data, {"variables", "rows"}, location="expected")
    variables, rows = data.get("variables"), data.get("rows")
    if not isinstance(variables, list) or not isinstance(rows, list):
        raise InputError("Expected answers require variables and rows arrays")
    if any(not isinstance(name, str) for name in variables):
        raise InputError("Expected SELECT variables must be strings")
    budget.consume("states", len(variables))
    decoded = []
    for row in rows:
        budget.consume("work")
        if not isinstance(row, list):
            raise InputError("Expected SELECT row must be an array")
        budget.consume("states", max(1, len(row)))
        decoded.append(tuple(read_term(term, {}) for term in row))
    return QueryAnswers(cast(tuple[str, ...], tuple(variables)), tuple(decoded))


def _decode(
    data: object, directory: Path, source: str, budget: Budget
) -> tuple[QueryRule, ...]:
    """Resolve each finite specification while keeping query text owned thereafter."""
    owned = own_json(
        data, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )
    budget.consume("bytes", len(json.dumps(owned, ensure_ascii=True).encode()))
    if not isinstance(owned, list):
        raise InputError("Query rule specification must be an array", location=source)
    rules = []
    names = set()
    for index, raw in enumerate(owned):
        budget.consume("resources")
        budget.consume("work")
        where = f"{source}/{index}"
        entry = object_value(raw, location=where)
        known_keys(
            entry, {"name", "query", "query_file", "scope", "expected"}, location=where
        )
        name = text_value(entry.get("name"), location=where + "/name")
        if name in names:
            raise InputError("Query rule names must be unique", location=where)
        names.add(name)
        if ("query" in entry) == ("query_file" in entry):
            raise InputError("Specify exactly one query or query_file", location=where)
        scope = object_value(entry.get("scope"), location=where + "/scope")
        known_keys(scope, {"kind", "graph_name"}, location=where + "/scope")
        kind = text_value(scope.get("kind"), location=where + "/scope/kind")
        graph_name = scope.get("graph_name")
        if graph_name is not None:
            graph_name = text_value(graph_name, location=where + "/scope/graph_name")
        selected = QueryScope(
            cast(Literal["default", "named", "dataset", "union"], kind), graph_name
        )
        query_source = source
        if "query_file" in entry:
            path = (
                directory / text_value(entry["query_file"], location=where)
            ).absolute()
            text, query_source = _read(path, budget), str(path)
        else:
            text = text_value(entry.get("query"), location=where + "/query")
            budget.consume("bytes", len(text.encode("utf-8")))
        rules.append(
            QueryRule(
                name,
                text,
                selected,
                _expected(entry.get("expected"), budget),
                query_source,
            )
        )
    return tuple(rules)


def query_rules_from_data(
    data: object, *, directory: Path, source: str = "", limits: Limits | None = None
) -> tuple[QueryRule, ...]:
    """Read a finite supplied JSON specification with an explicit local file base."""
    if not isinstance(directory, Path):
        raise InputError("Query rule files require an explicit Path directory")
    return _decode(data, directory, source, Budget(limits))


def load_query_rules(
    path: Path, *, limits: Limits | None = None
) -> tuple[QueryRule, ...]:
    """Read ordinary local JSON plus relative SPARQL files without discovery."""
    if not isinstance(path, Path):
        raise InputError("Query rule specifications require an explicit Path")
    budget = Budget(limits)
    text = _read(path, budget)
    data = decode_json(
        text,
        source=str(path),
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
    )
    return _decode(data, path.parent, str(path), budget)
