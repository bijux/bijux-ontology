"""Finite actual field associations from an original, already serialised document."""

from dataclasses import dataclass
from typing import Literal, cast

from rdflib import Literal as RdfLiteral

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.bindings import BindingIdentity, BindingObservation
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import (
    SourcePath,
    at_path,
    bound_path,
    location,
    validate_path,
)
from bijux_ontology.jsonld.evidence.entities import EntityKey, subject_key
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.bindings import source_entity, source_node_traits
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.emission.values import _literal_result, _matches
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry


@dataclass(frozen=True)
class SerializedFieldBinding:
    """Associate a business identity and intended field with one actual source key.

    Source usages select their own object. Reference/entity usages may select a
    named flat-graph entity while the absolute path identifies its actual authored
    source property. Paths are selections, never reconstructed output templates.
    """

    identity: BindingIdentity
    usage: ModelUsage
    field: str
    path: SourcePath

    def __post_init__(self) -> None:
        if not isinstance(self.identity, BindingIdentity) or not isinstance(
            self.usage, ModelUsage
        ):
            raise InputError(
                "Serialized bindings require typed identity and model usage"
            )
        if not isinstance(self.field, str) or not self.field.strip():
            raise InputError("Serialized bindings require an intended field name")
        validate_path(self.path)
        if not self.path or type(self.path[-1]) is not str:
            raise InputError("Select the actual source property, not a value subset")


@dataclass(frozen=True)
class SerializedObservations:
    """Finite sampled declarations and independently attributed binding evidence."""

    declarations: tuple[ModelDeclaration, ...]
    bindings: tuple[BindingObservation, ...]
    report: Report


def observed_value(
    item: ObservedOccurrence, model: str | None = None
) -> ValueDeclaration:
    """Describe actual maintained expansion traits without importing intended values."""
    traits = item.traits
    container = "@list" if item.container == "list" else None
    if traits.kind == "literal":
        return ValueDeclaration(
            "literal",
            datatype=traits.datatype if traits.direction is None else None,
            language=traits.language,
            direction=cast(Literal["ltr", "rtl"] | None, traits.direction),
            container=container,
        )
    if traits.kind in {"iri", "blank", "node"}:
        return ValueDeclaration(
            "object" if model or traits.kind != "iri" else "iri",
            types=traits.types,
            model=model,
            container=container,
        )
    return ValueDeclaration(container=container)


def _owner(
    prepared: PreparedUsages,
    usage: ModelUsage,
    path: SourcePath,
    budget: Budget,
) -> tuple[EntityKey, tuple[str, ...]] | None:
    """Require native source/entity correspondence before selecting any value."""
    try:
        node = at_path(prepared._document, path)
    except InputError:
        return None
    if not isinstance(node, dict):
        return None
    association = source_entity(node, path, prepared, budget)
    if association is None:
        return None
    if usage.reference is not None and association.identifier != usage.reference:
        return None
    if usage.graph is not ABSENT and (
        association.graph != usage.graph or association.anonymous_graph
    ):
        return None
    if usage.subject is not None:
        expected = usage.subject.expected_iri
        if expected is not None and association.identifier != expected:
            return None
        if not usage.subject.allow_blank and association.identifier is None:
            return None
    if usage.reference is not None or usage.entity:
        description = prepared.entities.get(association)
        if description is None or not description.described:
            return None
        return association, description.types
    if path != usage.path:
        return None
    traits = source_node_traits(node, path, prepared, budget)
    if len(traits) != 1 or traits[0].kind not in {"node", "iri", "blank"}:
        return None
    return association, traits[0].types


class _BindingObserver:
    """One operation owns actual source associations and literal observations."""

    def __init__(
        self,
        prepared: PreparedUsages,
        registry: dict[str, ModelDeclaration],
        budget: Budget,
        datatype_registry: DatatypeRegistry | None,
        rdf_direction: DirectionPolicy | None,
    ) -> None:
        self.prepared, self.registry, self.budget = prepared, registry, budget
        self.datatype_registry, self.rdf_direction = datatype_registry, rdf_direction
        self.owners: dict[
            tuple[ModelUsage, SourcePath], tuple[EntityKey, tuple[str, ...]] | None
        ] = {}
        self.native: dict[str, RdfLiteral] = {}

    def unknown(
        self, binding: SerializedFieldBinding, reason: str
    ) -> BindingObservation:
        """Retain omissions and unresolved selections as explicit incomplete scope."""
        return BindingObservation(
            binding.identity,
            Report(
                "serialization.binding",
                requested=1,
                complete=False,
                issues=(
                    Issue(
                        "serialization.binding_unverified",
                        reason,
                        location(binding.path),
                        kind="unverified",
                    ),
                ),
            ),
            reason,
        )

    def check_values(
        self,
        binding: SerializedFieldBinding,
        field: FieldDeclaration,
        values: tuple[ObservedOccurrence, ...],
    ) -> Report:
        """Compare actual mappings/values with independent intended field branches."""
        budget = self.budget
        issues = IssueCollector(budget)
        where = location(binding.path)
        checked, complete = 0, True
        if (values[0].predicate, values[0].reverse) != (field.predicate, field.reverse):
            issues.append(
                Issue(
                    "serialization.binding_predicate_mismatch",
                    "Actual mapping differs from the intended predicate",
                    where,
                )
            )
        for item in values:
            budget.consume("work", len(field.values) + 1)
            valid = None
            if item.traits.kind == "literal" and not item.empty:
                proof = _literal_result(
                    item,
                    where,
                    budget,
                    self.native,
                    datatype_registry=self.datatype_registry,
                    literal_contract=field.literal_contract,
                )
                retain_report(proof, budget, issues)
                valid = proof.verified
                complete = complete and proof.complete
            matches = tuple(
                _matches(
                    branch,
                    item,
                    self.registry,
                    self.prepared.entities,
                    self.rdf_direction,
                    budget=budget,
                    native_literals=self.native,
                    lexical_valid=valid,
                )
                for branch in field.values
            )
            if True not in matches:
                unknown = None in matches
                issues.append(
                    Issue(
                        "serialization.binding_value_unverified"
                        if unknown
                        else "serialization.binding_value_mismatch",
                        "Actual value does not establish an intended branch",
                        where,
                        kind="unverified" if unknown else "finding",
                    )
                )
                complete = complete and not unknown
            checked += 1
        return Report(
            "serialization.binding",
            checked,
            tuple(issues),
            requested=len(values),
            complete=complete,
        )

    def observe(
        self, binding: SerializedFieldBinding
    ) -> tuple[BindingObservation, FieldDeclaration | None, tuple[str, ...]]:
        """Keep one actual field's source/entity association and native evidence."""
        model = self.registry.get(binding.usage.model)
        field = (
            next((item for item in model.fields if item.name == binding.field), None)
            if model is not None
            else None
        )
        if field is None:
            raise InputError(
                "Binding selects a field absent from the intended inventory",
                location=location(binding.path),
            )
        parent = binding.path[:-1]
        cache_key = binding.usage, parent
        if cache_key not in self.owners:
            self.budget.consume("states")
            self.budget.consume("bytes", 128)
            self.owners[cache_key] = _owner(
                self.prepared, binding.usage, parent, self.budget
            )
        associated = self.owners[cache_key]
        if associated is None:
            return (
                self.unknown(binding, "source/entity correspondence is unresolved"),
                None,
                (),
            )
        node = at_path(self.prepared._document, parent)
        assert isinstance(node, dict)
        if binding.path[-1] not in node:
            return self.unknown(binding, "not serialized in this sample"), None, ()
        observed = self.prepared.observe(binding.path)
        if observed.limitation:
            return self.unknown(binding, observed.limitation), None, ()
        if not observed.occurrences:
            return self.unknown(binding, "no RDF value in this sample"), None, ()
        self.budget.consume("work", 2 * len(observed.occurrences))
        self.budget.consume("states", 3 * len(observed.occurrences))
        self.budget.consume("bytes", 384 * len(observed.occurrences))
        depth = min(item.depth for item in observed.occurrences)
        values = tuple(item for item in observed.occurrences if item.depth == depth)
        correspondence = all(
            subject_key(item) == associated[0]
            or item.predicate == "@id"
            and item.traits.identifier == associated[0].identifier
            for item in values
        )
        predicates = {(item.predicate, item.reverse) for item in values}
        if not correspondence or len(predicates) != 1:
            return (
                self.unknown(binding, "field owner or predicate is unresolved"),
                None,
                (),
            )
        if all(item.empty for item in values):
            return (
                self.unknown(binding, "empty collection has no sampled value branch"),
                None,
                (),
            )
        report = self.check_values(binding, field, values)
        predicate, reverse = next(iter(predicates))
        branches = tuple(
            dict.fromkeys(observed_value(item) for item in values if not item.empty)
        )
        declaration = FieldDeclaration(
            binding.field,
            predicate,
            branches,
            key=cast(str, binding.path[-1]),
            reverse=reverse,
        )
        return BindingObservation(binding.identity, report), declaration, associated[1]


def observe_serialized_bindings(
    prepared: PreparedUsages,
    models: tuple[ModelDeclaration, ...],
    bindings: tuple[SerializedFieldBinding, ...],
    *,
    budget: Budget,
    datatype_registry: DatatypeRegistry | None = None,
    rdf_direction: DirectionPolicy | None = None,
) -> SerializedObservations:
    """Observe finite actual associations; independent models remain authoritative.

    Missing, empty and unresolved selections cannot establish required bindings.
    Extracted declarations describe this sample only, never an unseen serializer
    inventory. Actual types/predicates are not copied from the intended models.
    """
    issues = IssueCollector(budget)
    results: list[BindingObservation] = []
    fields: dict[str, dict[str, FieldDeclaration]] = {}
    types: dict[str, tuple[str, ...]] = {}
    declarations: tuple[ModelDeclaration, ...] = ()
    exclusions: list[str] = []
    complete, requested = False, 1
    try:
        selected = capture_items(bindings, SerializedFieldBinding, budget)
        requested = len(selected)
        registry = index_models(models, budget, issues)
        observer = _BindingObserver(
            prepared, registry, budget, datatype_registry, rdf_direction
        )
        seen = set()
        for binding in selected:
            bound_text(binding.identity.owner, budget)
            bound_path(binding.identity.path, budget)
            bound_text(binding.field, budget)
            bound_text(binding.usage.model, budget)
            bound_path(binding.usage.path, budget)
            for value in (
                binding.usage.reference,
                binding.usage.graph,
                binding.usage.subject.expected_iri
                if binding.usage.subject is not None
                else None,
            ):
                if value is not None and value is not ABSENT:
                    bound_text(value, budget)
            bound_path(binding.path, budget)
            if binding.identity in seen:
                raise InputError(
                    "Selected application binding identities must be unique"
                )
            budget.consume("states", 4)
            budget.consume("bytes", 512)
            seen.add(binding.identity)
        for binding in selected:
            result, field, actual_types = observer.observe(binding)
            if field is not None:
                owned = fields.setdefault(binding.usage.model, {})
                previous = owned.get(binding.field)
                previous_types = types.get(binding.usage.model)
                if (
                    previous is not None
                    and previous != field
                    or previous_types is not None
                    and previous_types != actual_types
                ):
                    result = observer.unknown(
                        binding, "one model has differing sampled representations"
                    )
                else:
                    owned[binding.field] = field
                    types[binding.usage.model] = actual_types
            results.append(result)
            retain_report(result.report, budget, issues)
        selected_fields = {(item.usage.model, item.field) for item in selected}
        size = len(fields) + sum(
            len(items) + len(types.get(name, ())) for name, items in fields.items()
        )
        budget.consume("work", size)
        budget.consume("states", size)
        budget.consume("bytes", 256 * size)
        declarations = tuple(
            ModelDeclaration(name, types.get(name, ()), tuple(items.values()))
            for name, items in fields.items()
        )
        for model in registry.values():
            for field in model.fields:
                budget.consume("work")
                if (model.identifier, field.name) not in selected_fields:
                    budget.consume("states")
                    budget.consume(
                        "bytes", 128 + 4 * (len(model.identifier) + len(field.name))
                    )
                    exclusions.append(
                        f"unassociated model field: {model.identifier}.{field.name}"
                    )
        complete = all(item.report.complete for item in results)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "serialization.bindings_invalid",
                    str(error),
                    error.location,
                    kind="input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return SerializedObservations(
        declarations,
        tuple(results),
        Report(
            "serialization.observed",
            len(results),
            tuple(issues),
            requested=requested,
            complete=complete,
            allow_empty=requested == 0,
            children=tuple(item.report for item in results),
            exclusions=(
                *exclusions,
                *(item.exclusion for item in results if item.exclusion is not None),
            ),
        ),
    )
