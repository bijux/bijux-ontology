"""Finite maintained JSON Schema validation specialized for exact JSON numbers."""

from decimal import Decimal
from sys import getsizeof
from typing import Any

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.json.schema import DIALECT, InvalidSchema, backend, validator
from bijux_ontology.json.values import owned_json

from .decoding import decimal_bound, integral
from .locations import value_location


def _exact_schema(value: object, budget: Budget, depth: int = 0) -> object:
    """Copy qualified ordinary schema numbers without binary float comparisons.

    A supplied Python float has its ordinary JSON shortest decimal spelling;
    this establishes no stronger provenance for the original schema authoring.
    Nested dialect declarations are excluded because maintained evolve() would
    select the registered validator class rather than this local specialization.
    """
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Exact schema exceeds max_depth")
    if type(value) is float:
        result = Decimal(repr(value))
        decimal_bound(result, budget)
        return result
    if isinstance(value, dict):
        if depth and "$schema" in value:
            raise InputError("Nested schema dialect changes are not qualified")
        budget.consume("bytes", 64 + 32 * len(value))
        return {
            key: _exact_schema(child, budget, depth + 1) for key, child in value.items()
        }
    if isinstance(value, list):
        budget.consume("bytes", 64 + 8 * len(value))
        return [_exact_schema(child, budget, depth + 1) for child in value]
    return value


def _value_size(value: object, budget: Budget, depth: int = 0) -> int:
    """Prepay expanded maintained validation against every exact value node."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("Schema instance exceeds max_depth")
    if isinstance(value, Decimal):
        decimal_bound(value, budget)
        return getsizeof(value)
    if isinstance(value, str):
        bound_text(value, budget)
        return len(value) + 1
    if isinstance(value, list):
        return 1 + sum(_value_size(child, budget, depth + 1) for child in value)
    if isinstance(value, dict):
        size = 1
        for key, child in value.items():
            bound_text(key, budget)
            size += len(key) + _value_size(child, budget, depth + 1)
        return size
    return 1


def _engine(schema: object, budget: Budget) -> tuple[Any, int]:
    """Reuse generic schema qualification and public maintained extension APIs."""
    ordinary, _ = owned_json(schema, budget)
    engine, cost = validator(ordinary, budget)
    exact = _exact_schema(ordinary, budget)
    _, registry_type, specification = backend()
    from jsonschema.validators import extend  # type: ignore[import-untyped]

    def integer(checker: Any, value: object) -> bool:
        """JSON integer means mathematical integrality, excluding booleans."""
        budget.consume("work")
        if isinstance(value, Decimal):
            decimal_bound(value, budget)
            return integral(value)
        return type(value) is int

    def deny(uri: str) -> Any:
        """Keep exact schemas entirely within their owned local resource."""
        raise ResourceError("RDF JSON schemas never acquire external resources")

    specialized = extend(
        type(engine), type_checker=engine.TYPE_CHECKER.redefine("integer", integer)
    )
    if not isinstance(exact, dict):
        raise InputError("Expected an owned JSON Schema object")
    registry = registry_type(retrieve=deny).with_resource(
        exact["$id"], specification.create_resource(exact)
    )
    return specialized(exact, registry=registry), cost


def check_schema(
    value: object, schema: object, dialect: str | None, anchor: str, budget: Budget
) -> Report:
    """Return raw optional schema truth separately from exact literal identity.

    Only the shared finite 2020-12 keyword/reference profile is supported, without
    nested dialect declarations. multipleOf and all unqualified vocabulary are
    required-unknown. No processor, resolver or global validator is modified.
    """
    issues = IssueCollector(budget)
    checked, complete = 0, False
    try:
        if dialect is None:
            raise InputError("An explicit JSON Schema dialect is required")
        bound_text(dialect, budget)
        if dialect != DIALECT:
            raise InputError("Requested JSON Schema dialect is unavailable")
        backend()
        from referencing.exceptions import Unresolvable

        try:
            engine, cost = _engine(schema, budget)
            size = _value_size(value, budget)
            budget.consume("work", (size + 1) * cost)
            for error in engine.iter_errors(value):
                budget.consume("work")
                path = tuple(capture(error.absolute_path, budget))
                where, details = value_location(path, anchor, budget)
                issues.append(
                    Issue(
                        "literal.json_schema_mismatch",
                        "JSON value violates its declared schema",
                        where,
                        details=details,
                    )
                )
        except Unresolvable as error:
            raise ResourceError("JSON schema references are unavailable") from error
        checked, complete = 1, True
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("RDF JSON schema checking exceeded its operation limits")
    except InvalidSchema:
        issues.append(
            Issue(
                "literal.json_schema_invalid",
                "JSON schema is invalid in its selected dialect",
                anchor,
                kind="input_error",
            )
        )
    except (InputError, ResourceError, ValueError, TypeError, LookupError):
        try:
            issues.append(
                Issue(
                    "literal.json_schema_unverified",
                    "Requested schema needs bijux-ontology[jsonschema] and the "
                    "finite exact-number 2020-12 profile",
                    anchor,
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("RDF JSON schema diagnostic allowance exhausted")
    return Report(
        "rdf.json_schema",
        checked,
        tuple(issues),
        requested=1,
        complete=complete,
        regime="json-schema-2020-12-finite-exact-decimal",
    )
