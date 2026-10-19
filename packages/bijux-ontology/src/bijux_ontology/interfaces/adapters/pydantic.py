"""Pydantic field ownership and aliases without inferring RDF from Python types."""

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction, RdfField
from bijux_ontology.interfaces.adapters.pydantic_serialization import (
    check_instance as check_instance,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.reports import Issue, Report

if TYPE_CHECKING:
    from pydantic import BaseModel


def _field_contract(
    metadata: list[object],
    sidecar: RdfField | None,
    resolvers: Mapping[type[object], Callable[[object], RdfField | None]],
    location: str,
    budget: Budget,
    issues: IssueCollector,
) -> RdfField | None:
    """Apply explicit precedence while retaining contradictory mapping evidence."""
    annotated = [item for item in metadata if isinstance(item, RdfField)]
    if len(annotated) > 1:
        raise InputError("Field has multiple RDF contracts", location=location)
    candidates = ([sidecar] if sidecar is not None else []) + annotated
    for item in metadata:
        if isinstance(item, RdfField):
            continue
        for kind, resolver in resolvers.items():
            budget.consume("work")
            if not isinstance(item, kind):
                continue
            resolved = resolver(item)
            if resolved is not None:
                if not isinstance(resolved, RdfField):
                    raise InputError(
                        "Metadata resolver must return RdfField or None",
                        location=location,
                    )
                candidates.append(resolved)
    if not candidates:
        return None
    selected = candidates[0]
    identity = (selected.predicate, selected.reverse, selected.values)
    for candidate in candidates[1:]:
        budget.consume("work")
        other = (candidate.predicate, candidate.reverse, candidate.values)
        if identity != other:
            issues.append(
                Issue(
                    "adapter.mapping_conflict",
                    "Explicit field mappings disagree under the selected precedence",
                    location,
                    expected=repr(identity),
                    actual=repr(other),
                )
            )
    return selected


def model_declarations(
    models: Mapping[type["BaseModel"], tuple[str, ...]],
    *,
    identifiers: Mapping[type["BaseModel"], str] | None = None,
    metadata_resolvers: Mapping[type[object], Callable[[object], RdfField | None]]
    | None = None,
    field_contracts: Mapping[tuple[type["BaseModel"], str], RdfField] | None = None,
    by_alias: bool = True,
    exclude_none: bool = False,
    exclude_defaults: bool = False,
    exclude_unset: bool = False,
    limits: Limits | None = None,
) -> ModelExtraction:
    """Extract explicit annotations, metadata resolvers and sidecar contracts.

    Caller registries own RDF classes and nested model identifiers. Unmapped or
    computed output remains unverified. Known output contracts may deliberately
    describe custom serializers; no private framework schema is inspected.
    Sidecars keyed by (model, declared field name) precede annotated RdfField
    metadata, then metadata/resolver registration order. Contradictory mapping
    traits are reported even when a higher-priority contract is retained. Only
    the selected contract's explicit output/emission guarantees are used.
    """
    try:
        from pydantic import BaseModel
    except ImportError as error:
        raise ResourceError("Install bijux-ontology[pydantic]") from error
    if any(
        type(flag) is not bool
        for flag in (by_alias, exclude_none, exclude_defaults, exclude_unset)
    ):
        raise InputError("Serialization options must be booleans")
    declarations = []
    budget = Budget(limits if limits is not None else Limits())
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        resolvers = metadata_resolvers or {}
        sidecars = field_contracts or {}
        for kind, resolver in resolvers.items():
            budget.consume("work")
            if not isinstance(kind, type) or not callable(resolver):
                raise InputError("Metadata resolvers require a type and callable")
        for binding, sidecar_contract in sidecars.items():
            budget.consume("work")
            if (
                not isinstance(binding, tuple)
                or len(binding) != 2
                or not isinstance(binding[0], type)
                or not issubclass(binding[0], BaseModel)
                or binding[0] not in models
                or binding[1] not in binding[0].model_fields
                or not isinstance(sidecar_contract, RdfField)
            ):
                raise InputError(
                    "Sidecars require registered models and declared fields"
                )
        for cls, types in models.items():
            budget.consume("work")
            if not isinstance(cls, type) or not issubclass(cls, BaseModel):
                raise InputError("Pydantic registries require BaseModel classes")
            checked += 1
            identifier = (identifiers or {}).get(
                cls, f"{cls.__module__}.{cls.__qualname__}"
            )
            fields = []
            keys: set[str] = set()
            for name, field in cls.model_fields.items():
                budget.consume("work")
                checked += 1
                key = (
                    (field.serialization_alias or field.alias or name)
                    if by_alias
                    else name
                )
                location = f"{identifier}.{name}"
                contract = _field_contract(
                    field.metadata,
                    sidecars.get((cls, name)),
                    resolvers,
                    location,
                    budget,
                    issues,
                )
                if field.exclude is True:
                    if contract is not None and contract.emission == "guaranteed":
                        issues.append(
                            Issue(
                                "adapter.excluded_emission_conflict",
                                "Excluded field cannot guarantee an RDF occurrence",
                                location,
                            )
                        )
                    continue
                if key in keys:
                    raise InputError("Duplicate serialization alias", location=location)
                keys.add(key)
                if contract is None:
                    issues.append(
                        Issue(
                            "adapter.mapping_unverified",
                            "Serialized field has no explicit RDF contract",
                            location,
                            kind="unverified",
                        )
                    )
                    continue
                values = (
                    contract.values
                    if contract.output_contract
                    else (ValueDeclaration(),)
                )
                emission = contract.emission if contract.output_contract else "unknown"
                conditional = (
                    bool(getattr(field, "exclude_if", None))
                    or exclude_none
                    or exclude_defaults
                    or exclude_unset
                )
                if conditional and emission == "guaranteed":
                    emission = "optional"
                    issues.append(
                        Issue(
                            "adapter.emission_unverified",
                            "Conditional serialization can omit the declared field",
                            location,
                            kind="unverified",
                        )
                    )
                if not contract.output_contract:
                    issues.append(
                        Issue(
                            "adapter.output_unverified",
                            "Serializer output has not been explicitly described",
                            location,
                            kind="unverified",
                        )
                    )
                fields.append(
                    FieldDeclaration(
                        name,
                        contract.predicate,
                        values,
                        key=key,
                        reverse=contract.reverse,
                        emission=emission,
                        location=location,
                    )
                )
            for name, computed in cls.model_computed_fields.items():
                budget.consume("work")
                checked += 1
                key = computed.alias if by_alias and computed.alias else name
                location = f"{identifier}.{name}"
                if key in keys:
                    raise InputError(
                        "Duplicate computed serialization alias", location=location
                    )
                keys.add(key)
                issues.append(
                    Issue(
                        "adapter.computed_output_unverified",
                        "Computed output requires an explicit serializer contract",
                        location,
                        kind="unverified",
                    )
                )
            declarations.append(ModelDeclaration(identifier, types, tuple(fields)))
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return ModelExtraction(
        tuple(declarations),
        Report(
            "pydantic-extraction",
            checked,
            tuple(issues),
            complete=complete,
            requested=checked if complete else checked + 1,
            families=("explicit-serialization",),
        ),
    )
