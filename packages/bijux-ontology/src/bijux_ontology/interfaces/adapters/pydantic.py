"""Pydantic field ownership and aliases without inferring RDF from Python types."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, cast

from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.interfaces.adapters.contracts import ModelExtraction, RdfField
from bijux_ontology.interfaces.adapters.pydantic_contracts import (
    bound_contract,
    capture_mapping,
    joined,
)
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
    budget.consume("states", len(metadata))
    budget.consume("work", len(metadata))
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
                budget.consume("states")
                candidates.append(resolved)
    if not candidates:
        return None
    for candidate in candidates:
        bound_contract(candidate, budget)
    selected = candidates[0]
    identity = (selected.predicate, selected.reverse, selected.values)
    for candidate in candidates[1:]:
        budget.consume("work")
        other = (candidate.predicate, candidate.reverse, candidate.values)
        if identity != other:
            issues.append(
                Issue(
                    "adapter.mapping_conflict",
                    "Explicit field mapping predicates disagree"
                    if selected.predicate != candidate.predicate
                    else "Explicit field mapping directions disagree"
                    if selected.reverse != candidate.reverse
                    else "Explicit field mapping value alternatives disagree",
                    location,
                    expected=selected.predicate,
                    actual=candidate.predicate,
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
    serializer_contracts: Mapping[type["BaseModel"], ModelDeclaration] | None = None,
    by_alias: bool = True,
    exclude_none: bool = False,
    exclude_defaults: bool = False,
    exclude_unset: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ModelExtraction:
    """Extract explicit annotations, metadata resolvers and sidecar contracts.

    Caller registries own RDF classes and nested model identifiers. Unmapped or
    computed output remains unverified. Known output contracts may deliberately
    describe custom serializers; no private framework schema is inspected.
    Sidecars keyed by (model, declared field name) precede annotated RdfField
    metadata, then metadata/resolver registration order. Contradictory mapping
    traits are reported even when a higher-priority contract is retained. Only
    the selected contract's explicit output/emission guarantees are used.
    Field sidecars may also select public computed member names; their actual
    alias is retained separately from the original member identity. Whole-model
    serializer_contracts supply independent complete ModelDeclaration records,
    including nested model bindings, conditions and closed coverage. These must
    match the registered identity and RDF types and cannot compete with field
    sidecars. An open whole-output subset remains unverified until its existing
    EmissionCoverage explicitly describes field closure. Pass the returned
    extraction as check_instance's inventory to
    check one actual serialization; no private framework schemas are inspected.
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
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Pydantic extraction requires a Budget")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Pydantic limits conflict with the shared budget")
    budget = budget if budget is not None else Budget(limits)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        resolvers = metadata_resolvers or {}
        supplied_models = capture_mapping(
            cast(Mapping[object, tuple[str, ...]], models), budget
        )
        models = cast(Mapping[type[BaseModel], tuple[str, ...]], supplied_models)
        sidecars = capture_mapping(
            cast(Mapping[object, RdfField], field_contracts or {}), budget
        )
        whole = capture_mapping(
            cast(Mapping[object, ModelDeclaration], serializer_contracts or {}), budget
        )
        for model, whole_contract in whole.items():
            if (
                not isinstance(model, type)
                or not issubclass(model, BaseModel)
                or model not in models
                or not isinstance(whole_contract, ModelDeclaration)
            ):
                raise InputError(
                    "Serializer sidecars require registered models and declarations"
                )
            bound_contract(whole_contract, budget)
        for model in models:
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise InputError("Pydantic registries require BaseModel classes")
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
                or not isinstance(binding[1], str)
                or binding[1] not in binding[0].model_fields
                and binding[1] not in binding[0].model_computed_fields
                or not isinstance(sidecar_contract, RdfField)
            ):
                raise InputError(
                    "Sidecars require registered models and "
                    "declared fields or computed members"
                )
            bound_contract(sidecar_contract, budget)
            if binding[0] in whole:
                raise InputError("Whole-model and field sidecars cannot compete")
        for cls, types in models.items():
            budget.consume("work")
            if not isinstance(cls, type) or not issubclass(cls, BaseModel):
                raise InputError("Pydantic registries require BaseModel classes")
            checked += 1
            identifier = (identifiers or {}).get(cls)
            if identifier is None:
                identifier = joined(cls.__module__, cls.__qualname__, budget)
            if not isinstance(identifier, str):
                raise InputError("Pydantic model identifiers require text")
            bound_contract(identifier, budget)
            bound_contract(types, budget)
            if not isinstance(types, tuple) or any(
                not isinstance(item, str) for item in types
            ):
                raise InputError("Model types require an immutable IRI tuple")
            budget.consume("states", len(types))
            types = tuple(str(item) for item in types)
            identifier = str(identifier)
            if cls in whole:
                whole_contract = whole[cls]
                if str(whole_contract.identifier) != identifier or set(
                    whole_contract.types
                ) != set(types):
                    raise InputError(
                        "Serializer sidecar identity or RDF types conflict",
                        location=identifier,
                    )
                declarations.append(replace(whole_contract, identifier=identifier))
                if whole_contract.coverage is None:
                    issues.append(
                        Issue(
                            "adapter.output_unverified",
                            "Whole serializer sidecar is an open output subset",
                            identifier,
                            kind="unverified",
                        )
                    )
                continue
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
                location = joined(identifier, name, budget)
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
                bound_contract(key, budget)
                key = str(key)
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
                bound_contract(key, budget)
                key = str(key)
                location = joined(identifier, name, budget)
                if key in keys:
                    raise InputError(
                        "Duplicate computed serialization alias", location=location
                    )
                keys.add(key)
                contract = sidecars.get((cls, name))
                if contract is not None:
                    conditional = (
                        bool(getattr(computed, "exclude_if", None))
                        or exclude_none
                        or exclude_defaults
                        or exclude_unset
                    )
                    emission = (
                        contract.emission if contract.output_contract else "unknown"
                    )
                    if conditional and emission == "guaranteed":
                        emission = "optional"
                        issues.append(
                            Issue(
                                "adapter.emission_unverified",
                                "Conditional serialization can omit the computed field",
                                location,
                                kind="unverified",
                            )
                        )
                    if not contract.output_contract:
                        issues.append(
                            Issue(
                                "adapter.output_unverified",
                                "Computed serializer output has not been "
                                "explicitly described",
                                location,
                                kind="unverified",
                            )
                        )
                    fields.append(
                        FieldDeclaration(
                            name,
                            contract.predicate,
                            contract.values
                            if contract.output_contract
                            else (ValueDeclaration(),),
                            key=key,
                            reverse=contract.reverse,
                            emission=emission,
                            location=location,
                        )
                    )
                    continue
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
