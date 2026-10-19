"""Unambiguous identity indexing shared by model declaration and usage checks."""

from collections.abc import Sequence

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.reports import Issue


def index_models(
    models: Sequence[ModelDeclaration], budget: Budget, issues: IssueCollector
) -> dict[str, ModelDeclaration]:
    """Coalesce identical declarations and reject every ambiguous identity."""
    registry: dict[str, ModelDeclaration] = {}
    conflicts: set[str] = set()
    for model in models:
        budget.consume("work")
        if not isinstance(model, ModelDeclaration):
            raise InputError("Expected typed model declarations")
        if model.identifier in conflicts:
            continue
        if model.identifier in registry and registry[model.identifier] != model:
            issues.append(
                Issue(
                    "input.model_identity_conflict",
                    "Model identifier has conflicting declarations",
                    model.identifier,
                    kind="input_error",
                )
            )
            registry.pop(model.identifier)
            conflicts.add(model.identifier)
            continue
        registry[model.identifier] = model
    return registry
