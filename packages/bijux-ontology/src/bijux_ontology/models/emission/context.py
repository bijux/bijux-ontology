"""Operation-owned source and entity model binding discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from rdflib import Literal

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, IssueCollector
from bijux_ontology.json.paths import SourcePath, at_path, location
from bijux_ontology.jsonld.evidence.entities import EntityKey, object_key
from bijux_ontology.jsonld.evidence.entities import source_entity as source_record
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence
from bijux_ontology.jsonld.evidence.values import ValueTraits
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.usages.checking import PreparedUsages
from bijux_ontology.models.emission.bindings import (
    candidate_paths,
    same_contribution,
    source_entity,
    source_node_traits,
)
from bijux_ontology.models.emission.direction import DirectionCheck
from bijux_ontology.models.emission.matching import (
    identity_matches as _identity_matches,
)
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry

if TYPE_CHECKING:
    from bijux_ontology.models.corpus.types import _CorpusEvidence


def same_context(actual: object, expected: object, budget: Budget) -> bool:
    """Compare owned JSON without numeric coercion or changing context meaning."""
    budget.consume("work")
    if isinstance(actual, dict):
        if not isinstance(expected, dict):
            return False
        other = cast(dict[str, object], expected)
        if len(actual) != len(other):
            return False
        budget.reserve_storage(256)
        for key, value in actual.items():
            budget.consume("work", len(key) + 1)
            if key not in other or not same_context(value, other[key], budget):
                return False
        return True
    if isinstance(actual, list):
        if not isinstance(expected, list):
            return False
        items = cast(list[object], expected)
        if len(actual) != len(items):
            return False
        budget.reserve_storage(256)
        return all(
            same_context(value, items[index], budget)
            for index, value in enumerate(actual)
        )
    if isinstance(actual, str):
        if not isinstance(expected, str):
            return False
        budget.consume("work", len(actual) + len(expected) + 1)
        return bool(str.__eq__(actual, expected))
    if type(actual) is not type(expected):
        return False
    if type(actual) is int:
        budget.consume("work", actual.bit_length() // 8 + 1)
    return bool(actual == expected)


@dataclass
class EmissionContext:
    """One operation's binding discovery, shared evidence and cumulative counts."""

    snapshot: object
    prepared: PreparedUsages
    registry: dict[str, ModelDeclaration]
    bindings: list[ModelUsage]
    budget: Budget
    issues: IssueCollector
    native_literals: dict[str, Literal]
    direction_check: DirectionCheck
    role_checks: _RoleChecks | None
    rdf_direction: DirectionPolicy | None
    datatype_registry: DatatypeRegistry | None
    corpus: _CorpusEvidence | None
    checked: int = 0
    bound_traits: dict[SourcePath, tuple[ValueTraits, ...]] = field(
        default_factory=dict
    )
    bound_entities: dict[ModelUsage, EntityKey | None] = field(default_factory=dict)
    discovered_sources: dict[
        tuple[SourcePath, EntityKey | None, str], tuple[SourcePath, ...]
    ] = field(default_factory=dict)

    def traits_for(self, path: SourcePath) -> tuple[ValueTraits, ...]:
        """Reuse established own-node traits for explicit source bindings."""
        if path not in self.bound_traits:
            try:
                value = at_path(self.snapshot, path)
            except (KeyError, IndexError) as error:
                raise InputError(
                    "No supplied model usage", location=location(path)
                ) from error
            if not isinstance(value, dict):
                raise InputError(
                    "Model bindings select compact node objects",
                    location=location(path),
                )
            self.bound_traits[path] = source_node_traits(
                value, path, self.prepared, self.budget
            )
        return self.bound_traits[path]

    def entity_for(self, binding: ModelUsage) -> EntityKey | None:
        """Resolve each explicit source or entity binding within its owned graph."""
        if binding not in self.bound_entities:
            if binding.reference is not None:
                self.bound_entities[binding] = EntityKey(
                    binding.reference, graph=cast(str | None, binding.graph)
                )
            else:
                try:
                    node = at_path(self.snapshot, binding.path)
                except (KeyError, IndexError) as error:
                    raise InputError(
                        "No supplied model usage", location=location(binding.path)
                    ) from error
                self.bound_entities[binding] = (
                    source_entity(node, binding.path, self.prepared, self.budget)
                    if isinstance(node, dict)
                    else None
                )
        return self.bound_entities[binding]

    def identity_issues(
        self,
        binding: ModelUsage,
        model: ModelDeclaration,
        traits: ValueTraits,
        where: str,
    ) -> None:
        """Require all selected subject contracts without overriding model truth."""
        contracts = (
            (model.subject,)
            if binding.subject is None
            else (model.subject, binding.subject)
        )
        for contract in contracts:
            if not _identity_matches(contract, traits):
                self.issues.append(
                    Issue(
                        "emission.subject_mismatch",
                        "Subject violates the explicit RDF identity contract",
                        where,
                        expected=contract.expected_iri or "an IRI subject",
                        actual=traits.identifier or "anonymous subject",
                    )
                )
                break
        key = self.entity_for(binding) if binding.graph is not ABSENT else None
        if binding.graph is not ABSENT and (
            key is None or key.anonymous_graph or key.graph != binding.graph
        ):
            self.issues.append(
                Issue(
                    "emission.graph_unverified"
                    if key is None
                    else "emission.graph_mismatch",
                    "The required effective graph could not be established"
                    if key is None
                    else "Selected subject is outside the required effective graph",
                    where,
                    kind="unverified" if key is None else "finding",
                    expected=str(binding.graph),
                    actual=str(key.graph) if key is not None else "unestablished",
                )
            )

    def reference_binding(self, key: EntityKey, targets: set[str]) -> bool:
        """Require the referenced model identity in this exact effective graph."""
        for child in self.bindings:
            self.budget.consume("work")
            if (
                child.model in targets
                and (child.reference is not None or child.entity)
                and (self.entity_for(child) == key)
            ):
                return True
        return False

    def bind_reference(self, key: EntityKey, targets: set[str]) -> bool:
        """Follow one established named child in its exact selected graph."""
        if len(targets) != 1 or key.identifier is None or key.anonymous_graph:
            return False
        if key.identifier.startswith("_:"):
            return False
        description = self.prepared.entities.get(key)
        if description is None or not description.described:
            return False
        target = next(iter(targets))
        if target not in self.registry or set(self.registry[target].types) - set(
            description.types
        ):
            return False
        selected = ModelUsage(
            target, reference=key.identifier, graph=key.graph, entity=True
        )
        if selected not in self.bindings:
            self.budget.consume("states")
            self.bindings.append(selected)
        return True

    def bind_source(
        self, path: SourcePath, occurrence: ObservedOccurrence, targets: set[str]
    ) -> tuple[SourcePath, ...]:
        """Check proven inline descriptions without joining source guarantees."""
        if len(targets) != 1:
            return ()
        target = next(iter(targets))
        key = (path, object_key(occurrence), target)
        if key in self.discovered_sources:
            return self.discovered_sources[key]
        matches = []
        for candidate in candidate_paths(
            at_path(self.snapshot, path), path, self.budget
        ):
            traits = self.traits_for(candidate)
            if len(traits) != 1 or traits[0].kind not in {"node", "iri", "blank"}:
                continue
            node = at_path(self.snapshot, candidate)
            if not isinstance(node, dict):
                continue
            association = source_record(
                node, candidate, self.prepared.observe, self.budget
            )
            if association is None or not association.described:
                continue
            usage = self.prepared.observe(candidate)
            if association.key == object_key(occurrence) or (
                not usage.limitation
                and any(
                    same_contribution(occurrence, item) for item in usage.occurrences
                )
            ):
                matches.append(candidate)
        for selected_path in matches:
            if any(
                child.reference is None
                and (not child.entity)
                and (child.path == selected_path)
                and (child.model != target)
                for child in self.bindings
            ):
                self.issues.append(
                    Issue(
                        "emission.binding_conflict",
                        (
                            "Explicit child binding conflicts with the "
                            "established model relationship"
                        ),
                        location(selected_path),
                        kind="input_error",
                        expected=target,
                    )
                )
            selected = ModelUsage(target, selected_path)
            if selected not in self.bindings:
                self.budget.consume("states")
                self.bindings.append(selected)
        self.discovered_sources[key] = tuple(matches)
        return self.discovered_sources[key]

    def reference_source(self, path: SourcePath, key: EntityKey) -> bool:
        """Join descriptions only for actual references, not inline source nodes."""
        value = at_path(self.snapshot, path)
        if isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(item, str) for item in value)
        ):
            return True
        reference = False
        for candidate in candidate_paths(value, path, self.budget):
            node = at_path(self.snapshot, candidate)
            if not isinstance(node, dict):
                continue
            association = source_record(
                node, candidate, self.prepared.observe, self.budget
            )
            if association is not None and association.key == key:
                if association.described:
                    return False
                reference = True
        return reference
