"""Bounded exact JSON value contracts on one original opaque RDF literal."""

from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal

from rdflib import Literal
from rdflib.namespace import RDF

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import at_path, bound_index, bound_path
from bijux_ontology.rdf.terms import bound_node

from .decoding import _Unavailable, decimal_bound, decode_exact, equal_values, integral
from .locations import value_location
from .types import JsonLiteralCheck, JsonLiteralContract, JsonRequirement


@dataclass(frozen=True)
class _Requirement:
    """Qualified input path and owned exact comparison value."""

    requirement: JsonRequirement
    expected: object


def _requirements(
    contract: JsonLiteralContract, budget: Budget
) -> tuple[_Requirement, ...]:
    """Capture concrete typed requirements before any value checks."""
    result = []
    for item in capture(contract.requirements, budget):
        if not isinstance(item, JsonRequirement):
            raise InputError("Expected typed JsonRequirement declarations")
        bound_path(item.path, budget)
        if item.kind is not None:
            bound_text(item.kind, budget)
            if item.kind not in (
                "object",
                "array",
                "string",
                "integer",
                "number",
                "boolean",
                "null",
            ):
                raise InputError("Unsupported JSON value kind")
        for bound in (item.minimum, item.maximum):
            if bound is not None:
                decimal_bound(bound, budget)
        if (
            item.minimum is not None
            and item.maximum is not None
            and item.minimum > item.maximum
        ):
            raise InputError("JSON numeric minimum exceeds maximum")
        for count in (item.min_items, item.max_items):
            if count is not None:
                if type(count) is not int or count < 0:
                    raise InputError("JSON array bounds require nonnegative integers")
                bound_index(count, budget)
        if (
            item.min_items is not None
            and item.max_items is not None
            and item.min_items > item.max_items
        ):
            raise InputError("JSON array minimum exceeds maximum")
        expected = (
            decode_exact(item.expected_json, budget)
            if item.expected_json is not None
            else None
        )
        budget.consume("states")
        budget.consume("bytes", 96)
        result.append(_Requirement(item, expected))
    return tuple(result)


def _at(value: object, path: tuple[str | int, ...], budget: Budget) -> object:
    """Follow exact ordinary JSON keys/indexes without creating RDF observations."""
    for part in path:
        budget.consume("work")
        value = at_path(value, (part,))
    return value


def _kind(value: object, required: str, budget: Budget) -> bool:
    """Distinguish booleans from exact finite mathematical numbers."""
    budget.consume("work")
    if required in ("number", "integer"):
        if not isinstance(value, Decimal):
            return False
        decimal_bound(value, budget)
        return required == "number" or integral(value)
    return {
        "object": dict,
        "array": list,
        "string": str,
        "boolean": bool,
        "null": type(None),
    }[required] is type(value)


def _check(
    value: object,
    selected: _Requirement,
    anchor: str,
    budget: Budget,
    issues: IssueCollector,
) -> None:
    """Stream completed concrete violations before a later requirement can stop."""
    item = selected.requirement
    where, details = value_location(item.path, anchor, budget)
    try:
        actual = _at(value, item.path, budget)
    except (KeyError, IndexError):
        issues.append(
            Issue(
                "literal.json_mismatch",
                "Required JSON value is absent",
                where,
                details=details,
            )
        )
        return

    def tests() -> Iterator[bool]:
        """Stop on the first established violation before unrelated later work."""
        if item.kind is not None:
            yield _kind(actual, item.kind, budget)
        if item.expected_json is not None:
            yield equal_values(actual, selected.expected, budget)
        if item.minimum is not None or item.maximum is not None:
            if not isinstance(actual, Decimal):
                yield False
            else:
                decimal_bound(actual, budget)
                yield (item.minimum is None or actual >= item.minimum) and (
                    item.maximum is None or actual <= item.maximum
                )
        if item.min_items is not None or item.max_items is not None:
            yield (
                isinstance(actual, list)
                and (item.min_items is None or len(actual) >= item.min_items)
                and (item.max_items is None or len(actual) <= item.max_items)
            )

    if not all(tests()):
        issues.append(
            Issue(
                "literal.json_mismatch",
                "JSON value violates its declared requirement",
                where,
                details=details,
            )
        )


def check_json_literal(
    literal: Literal,
    contract: JsonLiteralContract,
    *,
    location: str = "literal",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> JsonLiteralCheck:
    """Check an opaque rdf:JSON value under strict syntax and exact number rules.

    Duplicate names, nonfinite constants and malformed JSON are input errors.
    Every number is a finite Decimal; no float conversion, global Decimal change,
    literal rewriting or invented RDF edge occurs. Parsed JSON equality ignores
    object order and numeric spelling, preserves array order and distinguishes
    booleans. It never establishes RDF term equality. Optional schemas use only
    the declared maintained finite 2020-12 profile and controlled local references.
    """
    if not isinstance(literal, Literal):
        raise InputError("JSON literal checking requires an actual RDF Literal")
    selected = operation(limits, budget)
    issues = IssueCollector(selected)
    original = None
    value = None
    decoded = False
    children: list[Report] = []
    checked, requested = 0, 1
    complete = False
    anchor = "literal"
    try:
        bound_text(location, selected)
        anchor = str(location)
        bound_node(literal, selected)
        original = literal
        if not isinstance(contract, JsonLiteralContract):
            raise InputError("Expected an explicit JsonLiteralContract")
        requirements = _requirements(contract, selected)
        requested += len(requirements) + int(
            contract.schema is not None or contract.dialect is not None
        )
        if literal.datatype != RDF.JSON or literal.language is not None:
            checked = 1
            issues.append(
                Issue(
                    "literal.json_mismatch",
                    "Selected literal requires datatype rdf:JSON",
                    anchor,
                )
            )
        else:
            selected.consume("resources")
            value = decode_exact(str(literal), selected)
            decoded = True
            checked = 1
            for requirement in requirements:
                _check(value, requirement, anchor, selected, issues)
                checked += 1
            if contract.schema is not None or contract.dialect is not None:
                from .schema import check_schema

                report = check_schema(
                    value, contract.schema, contract.dialect, anchor, selected
                )
                children.append(report)
                checked += 1
                retain_report(report, selected, issues)
            complete = checked == requested and all(
                child.complete for child in children
            )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("RDF JSON literal checking exceeded its operation limits")
    except InputError as error:
        try:
            pointer = error.location if isinstance(error.location, str) else ""
            bound_text(pointer, selected)
            size = len(anchor.encode("utf-8")) + 1 + len(pointer.encode("utf-8"))
            if size > selected.limits.max_bytes:
                raise LimitExceeded("Literal JSON error location exceeds max_bytes")
            selected.consume("bytes", size)
            where = anchor + "#" + pointer if pointer else anchor
            issues.append(
                Issue(
                    "literal.json_unverified"
                    if isinstance(error, _Unavailable)
                    else "literal.json_invalid",
                    "Exact JSON value support is unavailable"
                    if isinstance(error, _Unavailable)
                    else "Literal or contract is not valid strict JSON input",
                    where,
                    kind="unverified"
                    if isinstance(error, _Unavailable)
                    else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded:
            issues.exhausted("RDF JSON literal diagnostic allowance exhausted")
    return JsonLiteralCheck(
        original,
        Report(
            "rdf.json_literal",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
            regime="strict-json-exact-decimal-values",
        ),
        decoded,
        value,
    )
