"""Explicit consumer correspondence and owned representative release inputs."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType
from typing import TypeVar, cast

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
)
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.observations import ObservedUsage, SourcePath
from bijux_ontology.jsonld.selectors import _path_metadata
from bijux_ontology.limits import Budget
from bijux_ontology.models.corpus.checking import SerializerCase
from bijux_ontology.models.emission import ModelUsage
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.models.serialization import InstanceCheck
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.roles.classification import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.comparison import VocabularyChanges
from bijux_ontology.workflows.upgrades.anonymous import AnonymousAffectedUse
from bijux_ontology.workflows.upgrades.assessment import AffectedUse, ContractChange
from bijux_ontology.workflows.upgrades.facts import FactAffectedUse
from bijux_ontology.workflows.upgrades.uses import UseIndex

T = TypeVar("T")


def _sequence(values: Sequence[T], expected: type[T], budget: Budget) -> tuple[T, ...]:
    """Capture one finite required inventory with a single length witness."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Application inventories require finite typed sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError("Application inventory changed during capture") from error
        if not isinstance(value, expected):
            raise InputError("Application inventory has an unsupported item")
        result.append(value)
    return tuple(result)


def _mapping(
    values: Mapping[str, T], expected: type[T], budget: Budget
) -> Mapping[str, T]:
    """Own stable text identifiers without coercing unsupported input types."""
    if not isinstance(values, Mapping):
        raise InputError("Application identities require explicit finite mappings")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 64 * count)
    result: dict[str, T] = {}
    for key in values:
        budget.consume("work")
        if not isinstance(key, str) or not key:
            raise InputError("Application consumer identifiers must be nonempty text")
        bound_text(key, budget)
        value = values[key]
        if not isinstance(value, expected):
            raise InputError("Application identity has an unsupported value")
        normalized = str(key)
        if normalized in result or len(result) >= count:
            raise InputError("Application identities changed or conflict")
        result[normalized] = value
    if len(result) != count:
        raise InputError("Application identities changed during capture")
    return MappingProxyType(result)


def _json(value: object, budget: Budget) -> str:
    """Capture bounded actual JSON, independently of the caller's objects."""
    owned = own_json(
        value, max_bytes=budget.limits.max_bytes, max_depth=budget.limits.max_depth
    )
    result = json.dumps(
        owned, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    bound_text(result, budget)
    return result


@dataclass(frozen=True)
class ApplicationField:
    """One side's declaration and explicitly selected sample source occurrences.

    The enclosing mapping key is the stable cross-release field identity.
    Sample keys are actual SerializerCase/output identifiers; paths are complete
    field paths, never inferred parents or array-position correspondences.
    """

    model: str
    field: str
    samples: Mapping[str, SourcePath] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        """Own finite named bindings and concrete source paths."""
        budget = Budget()
        for value in (self.model, self.field):
            if not isinstance(value, str) or not value:
                raise InputError("Application fields require model and field names")
            bound_text(value, budget)
        captured = _mapping(self.samples, tuple, budget)
        for path in captured.values():
            if not path or any(
                type(part) not in (str, int) or isinstance(part, int) and part < 0
                for part in path
            ):
                raise InputError(
                    "Application field samples require nonempty source paths"
                )
            _path_metadata(path, budget)
        object.__setattr__(self, "model", str(self.model))
        object.__setattr__(self, "field", str(self.field))
        object.__setattr__(self, "samples", captured)


@dataclass(frozen=True, init=False)
class ApplicationOutput:
    """Already serialized actual output, with explicit model usage bindings."""

    document_json: str
    usages: tuple[ModelUsage, ...]

    def __init__(self, document: object, usages: Sequence[ModelUsage]) -> None:
        budget = Budget()
        object.__setattr__(self, "usages", _sequence(usages, ModelUsage, budget))
        object.__setattr__(self, "document_json", _json(document, budget))

    @property
    def document(self) -> object:
        """Export independent bytes' interpretation without a fake serializer."""
        return cast(object, json.loads(self.document_json))


@dataclass(frozen=True, init=False)
class ApplicationVersion:
    """An explicitly matched finite release, not a universal serializer promise.

    Mapping keys are caller-established correspondence IDs. Actual model/field
    names and IRIs may change independently. Instances and callable lifetimes
    remain caller-owned; options, outputs, contexts and resources are captured.
    """

    terms: Mapping[str, TermDeclaration]
    models: Mapping[str, ModelDeclaration]
    fields: Mapping[str, ApplicationField]
    role_uses: Mapping[str, RoleUse]
    cases: tuple[SerializerCase, ...]
    outputs: Mapping[str, ApplicationOutput]
    base: str | None
    rdf_direction: DirectionPolicy | None
    _context_json: str | None
    _resources_json: Mapping[str, str]

    def __init__(
        self,
        *,
        terms: Mapping[str, TermDeclaration] | None = None,
        models: Mapping[str, ModelDeclaration] | None = None,
        fields: Mapping[str, ApplicationField] | None = None,
        cases: Sequence[SerializerCase] = (),
        outputs: Mapping[str, ApplicationOutput] | None = None,
        role_uses: Mapping[str, RoleUse] | None = None,
        context: object | Absent = ABSENT,
        resources: Mapping[str, object] | None = None,
        base: str | None = None,
        rdf_direction: DirectionPolicy | None = None,
    ) -> None:
        budget = Budget()
        for name, values, expected in (
            ("terms", terms, TermDeclaration),
            ("models", models, ModelDeclaration),
            ("fields", fields, ApplicationField),
            ("outputs", outputs, ApplicationOutput),
            ("role_uses", role_uses, RoleUse),
        ):
            object.__setattr__(
                self,
                name,
                _mapping(values if values is not None else {}, expected, budget),
            )
        captured = _sequence(cases, SerializerCase, budget)
        names = set(self.outputs)
        for case in captured:
            bound_text(case.identifier, budget)
            if str(case.identifier) in names:
                raise InputError("Application sample identifiers must be unique")
            names.add(str(case.identifier))
        registry = {model.identifier: model for model in self.models.values()}
        if len(registry) != len(self.models):
            raise InputError("Application models require unique actual identifiers")
        for binding in self.fields.values():
            model = registry.get(binding.model)
            if model is None or not any(f.name == binding.field for f in model.fields):
                raise InputError(
                    "Application field is outside the explicit model registry"
                )
            if set(binding.samples) - names:
                raise InputError("Application field references an absent sample")
        if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
            raise InputError("Invalid application RDF direction policy")
        if base is not None:
            bound_text(base, budget)
            absolute_iri(base)
        supplied = _mapping(resources if resources is not None else {}, object, budget)
        snapshots = {}
        for url, value in supplied.items():
            absolute_iri(url)
            snapshots[url] = _json(value, budget)
        object.__setattr__(self, "cases", captured)
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "rdf_direction", rdf_direction)
        object.__setattr__(
            self, "_context_json", None if context is ABSENT else _json(context, budget)
        )
        object.__setattr__(self, "_resources_json", MappingProxyType(snapshots))

    @property
    def context(self) -> object | Absent:
        """Export an independent explicit context, including JSON null."""
        return ABSENT if self._context_json is None else json.loads(self._context_json)

    @property
    def resources(self) -> Mapping[str, object]:
        """Export independent closed resource snapshots for this version only."""
        return {url: json.loads(value) for url, value in self._resources_json.items()}


@dataclass(frozen=True)
class ApplicationDeclarationChange:
    """Explicit matched consumer declaration change, independent of check failures."""

    consumer: str
    kind: str
    aspects: tuple[str, ...]
    before: TermDeclaration | ModelDeclaration | FieldDeclaration | RoleUse
    after: TermDeclaration | ModelDeclaration | FieldDeclaration | RoleUse


@dataclass(frozen=True)
class ApplicationValueChange:
    """Selected expanded literal representations, distinct from RDF value equality."""

    consumer: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True)
class ApplicationSampleComparison:
    """Original checked outputs, selected field witnesses and exact RDF comparison."""

    identifier: str
    before: InstanceCheck | None
    after: InstanceCheck | None
    observations: tuple[tuple[str, ObservedUsage | None, ObservedUsage | None], ...]
    values: tuple[ApplicationValueChange, ...]
    mapping: Report
    dataset: Report


@dataclass(frozen=True)
class ApplicationUpgrade:
    """Raw old/new truth, explicit changes and role-qualified ontology impacts.

    The aggregate retains baseline failures even when the candidate fixes them.
    Neither declaration nor output changes automatically impose caller policy.
    """

    before: Report
    after: Report
    declarations: tuple[ApplicationDeclarationChange, ...]
    samples: tuple[ApplicationSampleComparison, ...]
    contracts: tuple[ContractChange, ...]
    changes: VocabularyChanges | None
    before_uses: UseIndex
    after_uses: UseIndex
    before_affected: tuple[AffectedUse, ...]
    after_affected: tuple[AffectedUse, ...]
    before_fact_uses: tuple[FactAffectedUse, ...]
    after_fact_uses: tuple[FactAffectedUse, ...]
    report: Report
    before_anonymous_uses: tuple[AnonymousAffectedUse, ...] = dataclass_field(
        default=(), kw_only=True
    )
    after_anonymous_uses: tuple[AnonymousAffectedUse, ...] = dataclass_field(
        default=(), kw_only=True
    )
    change_qualification: Report | None = dataclass_field(default=None, kw_only=True)

    @property
    def candidate_report(self) -> Report:
        """Inspect current contract truth without erasing the raw baseline."""
        return self.after

    @property
    def demonstrated_break(self) -> bool:
        """Require an established newly failing matched consumer obligation."""
        return any(
            change.status == "introduced"
            and change.after is not None
            and change.after.kind == "finding"
            and change.after.severity == "error"
            for change in self.contracts
        )
