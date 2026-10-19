"""TypedDict Annotated output contracts with explicit finite model registries."""

import sys
import typing
from collections.abc import Mapping
from types import SimpleNamespace
from typing import (
    Annotated,
    NotRequired,
    Required,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction, RdfField
from bijux_ontology.interfaces.adapters.field_contracts import (
    declare_field,
    select_alias,
    select_contract,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits


def _contract(annotation: object, *, location: str) -> RdfField | None:
    metadata: list[RdfField] = []
    while True:
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation, *extras = get_args(annotation)
            metadata.extend(item for item in extras if isinstance(item, RdfField))
        elif (
            origin in (Required, NotRequired, getattr(typing, "ReadOnly", None))
            and origin is not None
        ):
            annotation = get_args(annotation)[0]
        else:
            break
    if len(metadata) > 1:
        raise InputError("Field has multiple RDF contracts", location=location)
    return metadata[0] if metadata else None


def model_declarations(
    models: Mapping[type, tuple[str, ...]],
    *,
    identifiers: Mapping[type, str] | None = None,
    aliases: Mapping[type, Mapping[str, str]] | None = None,
    field_contracts: Mapping[type, Mapping[str, RdfField]] | None = None,
    limits: Limits | None = None,
) -> ModelExtraction:
    """Extract ``Annotated[..., RdfField(...)]`` including inherited definitions.

    Alias and RDF serializer sidecars use explicit field-name mappings. Public
    typing resolves forward annotations against the supplied registry and the
    defining module; no referenced model is recursively discovered. Unresolved
    metadata stays unverified unless a sidecar supplies its actual output.
    Required/NotRequired and Python types never imply RDF emission or kinds.
    """
    if not isinstance(models, Mapping):
        raise InputError("TypedDict registries require named model mappings")
    if any(not is_typeddict(cls) for cls in models):
        raise InputError("TypedDict registries require TypedDict classes")
    selected_limits = limits or Limits()
    if len(models) > selected_limits.max_states:
        raise LimitExceeded("TypedDict registry exceeds max_states")
    names = {
        cls: (identifiers or {}).get(cls, f"{cls.__module__}.{cls.__qualname__}")
        for cls in models
    }
    local = {cls.__name__: cls for cls in models}
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
            if not is_typeddict(cls):
                raise InputError("TypedDict registries require TypedDict classes")
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
            public = dict(cls.__annotations__)
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
                annotation = public.get(name)
                contract = _contract(annotation, location=location)
                if contract is None and annotation is not None:
                    holder = SimpleNamespace(__annotations__={name: annotation})
                    module = sys.modules.get(cls.__module__)
                    try:
                        resolved = get_type_hints(
                            holder,
                            globalns=vars(module) if module is not None else {},
                            localns=local,
                            include_extras=True,
                        )
                    except (
                        NameError,
                        TypeError,
                        AttributeError,
                        SyntaxError,
                        RecursionError,
                    ):
                        resolved = {}
                    contract = _contract(resolved.get(name), location=location)
                key = select_alias(
                    None, renames.get(name), name=name, location=location, issues=issues
                )
                if key in keys:
                    raise InputError("Duplicate serialization alias", location=location)
                keys.add(key)
                chosen = select_contract(
                    contract, supplied.get(name), location=location, issues=issues
                )
                field = declare_field(
                    name,
                    key,
                    chosen,
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
            "typed-dict-extraction",
            checked,
            tuple(issues),
            complete=complete,
            requested=checked if complete else checked + 1,
            families=("explicit-serialization",),
        ),
    )
