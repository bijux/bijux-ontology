"""Public dataclass metadata extraction without inferring RDF from Python types."""

from collections.abc import Mapping
from dataclasses import fields, is_dataclass

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology_adapters.contracts import ModelExtraction, RdfField
from bijux_ontology_adapters.field_contracts import (
    declare_field,
    select_alias,
    select_contract,
)


def model_declarations(
    models: Mapping[type, tuple[str, ...]],
    *,
    identifiers: Mapping[type, str] | None = None,
    aliases: Mapping[type, Mapping[str, str]] | None = None,
    field_contracts: Mapping[type, Mapping[str, RdfField]] | None = None,
    limits: Limits | None = None,
) -> ModelExtraction:
    """Extract ``field(metadata={"rdf": RdfField(...), "alias": "output_key"})``.

    Explicit serializer sidecars take precedence; conflicting metadata remains a
    finding. ``ValueDeclaration.model`` refers only to registered identifiers.
    Inheritance uses public ``dataclasses.fields``. Defaults, required arguments
    and Python annotations never establish RDF kinds or emission guarantees.
    """
    if not isinstance(models, Mapping):
        raise InputError("Dataclass registries require named model mappings")
    if any(not isinstance(cls, type) or not is_dataclass(cls) for cls in models):
        raise InputError("Dataclass registries require dataclass classes")
    selected_limits = limits or Limits()
    if len(models) > selected_limits.max_states:
        raise LimitExceeded("Dataclass registry exceeds max_states")
    names = {
        cls: (identifiers or {}).get(cls, f"{cls.__module__}.{cls.__qualname__}")
        for cls in models
    }
    if any(not isinstance(name, str) or not name.strip() for name in names.values()):
        raise InputError("Model identifiers require nonempty strings")
    registered = frozenset(names.values())
    declarations = []
    budget = Budget(selected_limits)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        for cls, types in models.items():
            budget.consume("work")
            if not isinstance(cls, type) or not is_dataclass(cls):
                raise InputError("Dataclass registries require dataclass classes")
            identifier = names[cls]
            checked += 1
            if not types:
                issues.append(
                    Issue(
                        "adapter.class_mapping_unverified",
                        "Model has no explicit RDF classes",
                        identifier,
                        kind="unverified",
                    )
                )
            supplied = dict((field_contracts or {}).get(cls, {}))
            renames = dict((aliases or {}).get(cls, {}))
            public = {item.name: item for item in fields(cls)}
            selected = tuple(dict.fromkeys((*public, *supplied)))
            if set(renames) - set(selected):
                raise InputError(
                    "Alias names an unknown output field", location=identifier
                )
            output = []
            keys: set[str] = set()
            for name in selected:
                budget.consume("work")
                checked += 1
                location = f"{identifier}.{name}"
                metadata = public[name].metadata if name in public else {}
                key = select_alias(
                    metadata.get("alias"),
                    renames.get(name),
                    name=name,
                    location=location,
                    issues=issues,
                )
                if key in keys:
                    raise InputError("Duplicate serialization alias", location=location)
                keys.add(key)
                contract = select_contract(
                    metadata.get("rdf"),
                    supplied.get(name),
                    location=location,
                    issues=issues,
                )
                field = declare_field(
                    name,
                    key,
                    contract,
                    identifiers=registered,
                    location=location,
                    issues=issues,
                )
                if field is not None:
                    output.append(field)
            declarations.append(ModelDeclaration(identifier, types, tuple(output)))
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return ModelExtraction(
        tuple(declarations),
        Report(
            "dataclass-extraction",
            checked,
            tuple(issues),
            complete=complete,
            requested=checked if complete else checked + 1,
            families=("explicit-serialization",),
        ),
    )
