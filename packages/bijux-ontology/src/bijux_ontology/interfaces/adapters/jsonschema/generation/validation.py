"""Bounded validation of generated output through the shared schema engine."""

import json

from bijux_ontology._json import owned_json as owned_json
from bijux_ontology._jsonpaths import bounded_location
from bijux_ontology._jsonschema import (
    DIALECT as DIALECT,
)
from bijux_ontology._jsonschema import (
    backend as backend,
)
from bijux_ontology._jsonschema import (
    validator as validator,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import operation as operation
from bijux_ontology.execution.inputs import capture as capture
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.reports import Issue, Report


def validate_output(
    schema_json: str | None,
    output: object,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Report JSON conformance separately from the RDF translation's limitations."""
    backend()
    from referencing.exceptions import Unresolvable

    selected = operation(limits, budget)
    issues = IssueCollector(selected)
    complete = False
    checked = 0
    try:
        if schema_json is None:
            raise InputError("No generated schema is available")
        bound_text(schema_json, selected)
        schema, _ = owned_json(json.loads(schema_json), selected)
        instance, size = owned_json(output, selected)
        engine, cost = validator(schema, selected)
        selected.consume("work", (size + 1) * cost)
        for error in engine.iter_errors(instance):
            selected.consume("work")
            where = bounded_location(
                tuple(capture(error.absolute_path, selected)), selected
            )
            issues.append(
                Issue(
                    "schema.output_invalid",
                    "Actual serialized output violates the generated JSON schema",
                    where,
                )
            )
        checked = 1
        complete = True
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Generated schema validation exceeded its limits")
    except (InputError, ValueError, TypeError, LookupError, Unresolvable):
        try:
            issues.append(
                Issue(
                    "schema.output_unverified",
                    "Output or generated schema could not be qualified",
                    kind="unverified",
                )
            )
        except LimitExceeded:
            issues.exhausted("Generated schema diagnostics exceeded their limit")
    return Report(
        "jsonschema.output", checked, tuple(issues), requested=1, complete=complete
    )
