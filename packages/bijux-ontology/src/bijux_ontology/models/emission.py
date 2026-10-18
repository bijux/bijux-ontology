"""Explicit representative JSON-LD output compared with model declarations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import KW_ONLY, dataclass, replace
from typing import TYPE_CHECKING, cast

from rdflib import RDF, Literal, URIRef

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import (
    ModelDeclaration,
    SubjectIdentity,
    ValueDeclaration,
)
from bijux_ontology.jsonld.checking import prepare_usages
from bijux_ontology.jsonld.entities import (
    EntityInventory,
    EntityKey,
    object_key,
    subject_key,
)
from bijux_ontology.jsonld.entities import (
    source_entity as source_record,
)
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    SourcePath,
    at_path,
    location,
)
from bijux_ontology.jsonld.resources import DocumentLoader, OfflineDocumentLoader
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.jsonld.values import ValueTraits
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.terms import literal_term, term_key

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .bindings import (
    candidate_paths,
    same_contribution,
    source_entity,
    source_node_traits,
)
from .coverage import owns, predicate_issues, source_issues, source_properties
from .emission_groups import TermKey, check_groups
from .occurrences import check_occurrences
from .registry import index_models
from .value_matching import identity_matches as _identity_matches
from .value_matching import matches_value

if TYPE_CHECKING:
    from .corpus.types import _CorpusEvidence


@dataclass(frozen=True)
class ModelUsage:
    """Bind a source object or an explicitly graph-qualified named reference.

    ``subject`` constrains the source object's effective identity. ``reference``
    selects an entity directly; its path is only an application diagnostic anchor.
    ``graph=ABSENT`` follows a source binding, None requires the default graph.
    Named references require an explicit graph. ``entity`` joins descriptions in
    that graph; ordinary source bindings retain each object's field guarantees.
    ``closed`` makes an absent referenced description a finding instead of unknown.
    """

    model: str
    path: SourcePath = ()
    _: KW_ONLY
    subject: SubjectIdentity | None = None
    reference: str | None = None
    graph: str | Absent | None = ABSENT
    entity: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise InputError("Model usage requires a model identifier")
        path = tuple(self.path)
        if isinstance(cast(object, self.path), (str, bytes)) or any(
            type(part) not in (str, int) or isinstance(part, int) and part < 0
            for part in path
        ):
            raise InputError("Model usage requires a key/index path")
        object.__setattr__(self, "path", path)
        if self.subject is not None and not isinstance(self.subject, SubjectIdentity):
            raise InputError("Expected a typed subject identity contract")
        if self.graph is not None and self.graph is not ABSENT:
            absolute_iri(self.graph, location="graph")
        if type(self.entity) is not bool or type(self.closed) is not bool:
            raise InputError("entity and closed must be booleans")
        if self.reference is not None:
            absolute_iri(self.reference, location="reference")
            if self.graph is ABSENT:
                raise InputError("Named reference bindings require an explicit graph")
            if self.subject is not None and self.subject.expected_iri not in (
                None,
                self.reference,
            ):
                raise InputError("Reference and expected subject identities conflict")


def _matches(
    value: ValueDeclaration,
    occurrence: ObservedOccurrence,
    registry: dict[str, ModelDeclaration],
    inventory: EntityInventory | None = None,
) -> bool | None:
    """Resolve actual joined descriptions before shared scalar contract matching."""
    traits = occurrence.traits
    if (
        inventory is not None
        and value.kind in {"iri", "object"}
        and traits.kind in {"iri", "blank", "node"}
    ):
        key = object_key(occurrence)
        description = inventory.get(key) if key is not None else None
        if description is not None and description.described:
            traits = replace(traits, types=description.types)
    return matches_value(
        value, traits, registry, container=occurrence.container, empty=occurrence.empty
    )


def _literal_result(
    occurrence: ObservedOccurrence,
    where: str,
    budget: Budget,
    native: dict[str, Literal],
) -> Report:
    """Validate actual lexical values, obtaining native spelling from PyLD itself."""
    raw = occurrence.value
    if not isinstance(raw, dict) or "@value" not in raw:
        raise RuntimeError("Observed literal lacks its expanded value")
    value = raw["@value"]
    datatype, language = occurrence.traits.datatype, occurrence.traits.language
    if datatype is not None:
        absolute_iri(datatype)
    if language is not None:
        language_tag(language)
    if raw.get("@type") == "@json":
        return Report(
            "literal",
            1,
            (
                Issue(
                    "literal.datatype_unverified",
                    "JSON literal lexical validation is unsupported",
                    where,
                    kind="unverified",
                    actual=datatype,
                ),
            ),
        )
    try:
        if isinstance(value, str):
            term = literal_term(value, datatype=datatype, language=language)
        elif type(value) in (int, float, bool):
            key = occurrence.value_json
            if key not in native:
                budget.consume("states")
                loader = OfflineDocumentLoader({})
                jsonld = processor()
                dataset = jsonld.to_rdf(
                    [{"urn:bijux:observed-literal": [raw]}],
                    {
                        "documentLoader": loader,
                        "contextResolver": jsonld.ContextResolver({}, loader),
                    },
                )
                # This wrapper supplies only an already-expanded literal. Its
                # canonical native lexical form comes from the public processor.
                statements = dataset.get("@default", [])
                if len(statements) != 1:
                    raise RuntimeError("Native literal did not produce one RDF term")
                actual = statements[0]["object"]
                if actual.get("type") != "literal":
                    raise RuntimeError("Native value did not produce an RDF literal")
                native[key] = literal_term(
                    actual["value"],
                    datatype=actual.get("datatype"),
                    language=actual.get("language"),
                )
            term = native[key]
        else:
            raise RuntimeError("Unexpected expanded literal value")
    except InputError as error:
        return Report(
            "literal",
            1,
            (
                Issue(
                    "literal.lexical_unverified",
                    str(error),
                    where,
                    kind="unverified",
                    actual=datatype,
                ),
            ),
        )
    return check_literal(term, location=where)


def _group_terms(
    items: Sequence[ObservedOccurrence], native: dict[str, Literal], budget: Budget
) -> tuple[TermKey, ...] | None:
    """Read exact discriminator terms; keep list heads separate from members."""
    keys = []
    for item in items:
        budget.consume("work")
        if item.container == "list":
            if item.empty:
                keys.append(term_key(RDF.nil))
                continue
            return None
        traits = item.traits
        if traits.identifier_kind == "iri" and traits.identifier is not None:
            keys.append(term_key(URIRef(traits.identifier)))
        elif traits.kind == "literal":
            raw = item.value
            if not isinstance(raw, dict) or "@value" not in raw:
                return None
            if isinstance(raw["@value"], str):
                term = literal_term(
                    raw["@value"], datatype=traits.datatype, language=traits.language
                )
            else:
                if item.value_json not in native:
                    result = _literal_result(item, "", budget, native)
                    if not result.verified:
                        return None
                cached = native.get(item.value_json)
                if cached is None:
                    return None
                term = cached
            keys.append(term_key(term))
        else:
            return None
    return tuple(dict.fromkeys(keys))


def check_emission(
    document: object,
    models: Sequence[ModelDeclaration],
    usages: Sequence[ModelUsage],
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
    budget: Budget | None = None,
    _corpus: _CorpusEvidence | None = None,
) -> Report:
    """Check selected actual outputs, without claiming all hypothetical instances.

    Each occurrence must match one alternative branch. Nested model nodes need
    established source or graph-qualified entity bindings. Supported unambiguous
    children are discovered automatically; explicit bindings can constrain them.
    Unsupplied descriptions
    stay unknown unless their reference binding explicitly requires closed coverage.
    Predicates, reversal, collection membership and literal traits come from the
    shared standards-processed usage snapshot, never a second expansion engine.
    Type checks compare explicit emitted types, without ontology entailment.
    """
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    selected = limits or (budget.limits if budget is not None else Limits())
    budget = budget if budget is not None else Budget(selected)
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    native_literals: dict[str, Literal] = {}
    try:
        registry = index_models(models, budget, issues)
        for declaration in registry.values():
            for field in declaration.fields:
                for index, branch in enumerate(field.values):
                    budget.consume("work")
                    if branch.model is not None and branch.model not in registry:
                        issues.append(
                            Issue(
                                "input.model_reference_missing",
                                "Value branch references an unknown model",
                                f"{declaration.identifier}.{field.name}[{index}]",
                                kind="input_error",
                                actual=branch.model,
                            )
                        )
        snapshot = own_json(
            document, max_bytes=selected.max_bytes, max_depth=selected.max_depth
        )
        prepared = prepare_usages(
            snapshot,
            base=base,
            document_loader=document_loader,
            allowed_drops=allowed_drops,
            limits=selected,
            budget=budget,
        )
        bindings = list(usages)
        bound_traits: dict[SourcePath, tuple[ValueTraits, ...]] = {}

        def traits_for(path: SourcePath) -> tuple[ValueTraits, ...]:
            """Reuse established own-node traits for explicit source bindings."""
            if path not in bound_traits:
                try:
                    value = at_path(snapshot, path)
                except (KeyError, IndexError) as error:
                    raise InputError(
                        "No supplied model usage", location=location(path)
                    ) from error
                if not isinstance(value, dict):
                    raise InputError(
                        "Model bindings select compact node objects",
                        location=location(path),
                    )
                bound_traits[path] = source_node_traits(value, path, prepared, budget)
            return bound_traits[path]

        bound_entities: dict[ModelUsage, EntityKey | None] = {}

        def entity_for(binding: ModelUsage) -> EntityKey | None:
            """Resolve each explicit source or entity binding within its owned graph."""
            if binding not in bound_entities:
                if binding.reference is not None:
                    bound_entities[binding] = EntityKey(
                        binding.reference, graph=cast(str | None, binding.graph)
                    )
                else:
                    try:
                        node = at_path(snapshot, binding.path)
                    except (KeyError, IndexError) as error:
                        raise InputError(
                            "No supplied model usage", location=location(binding.path)
                        ) from error
                    bound_entities[binding] = (
                        source_entity(node, binding.path, prepared, budget)
                        if isinstance(node, dict)
                        else None
                    )
            return bound_entities[binding]

        def identity_issues(
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
                    issues.append(
                        Issue(
                            "emission.subject_mismatch",
                            "Subject violates the explicit RDF identity contract",
                            where,
                            expected=contract.expected_iri or "an IRI subject",
                            actual=traits.identifier or "anonymous subject",
                        )
                    )
                    break
            key = entity_for(binding) if binding.graph is not ABSENT else None
            if binding.graph is not ABSENT and (
                key is None or key.anonymous_graph or key.graph != binding.graph
            ):
                issues.append(
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

        def reference_binding(key: EntityKey, targets: set[str]) -> bool:
            """Require the referenced model identity in this exact effective graph."""
            for child in bindings:
                budget.consume("work")
                if (
                    child.model in targets
                    and (child.reference is not None or child.entity)
                    and entity_for(child) == key
                ):
                    return True
            return False

        def bind_reference(key: EntityKey, targets: set[str]) -> bool:
            """Follow one established named child in its exact selected graph."""
            if len(targets) != 1 or key.identifier is None or key.anonymous_graph:
                return False
            if key.identifier.startswith("_:"):
                return False
            description = prepared.entities.get(key)
            if description is None or not description.described:
                return False
            target = next(iter(targets))
            if target not in registry or set(registry[target].types) - set(
                description.types
            ):
                return False
            selected = ModelUsage(
                target, reference=key.identifier, graph=key.graph, entity=True
            )
            if selected not in bindings:
                budget.consume("states")
                bindings.append(selected)
            return True

        discovered_sources: dict[
            tuple[SourcePath, EntityKey | None, str], tuple[SourcePath, ...]
        ] = {}

        def bind_source(
            path: SourcePath, occurrence: ObservedOccurrence, targets: set[str]
        ) -> tuple[SourcePath, ...]:
            """Check proven inline descriptions without joining source guarantees."""
            if len(targets) != 1:
                return ()
            target = next(iter(targets))
            key = (path, object_key(occurrence), target)
            if key in discovered_sources:
                return discovered_sources[key]
            matches = []
            for candidate in candidate_paths(at_path(snapshot, path), path, budget):
                traits = traits_for(candidate)
                if len(traits) != 1 or traits[0].kind not in {"node", "iri", "blank"}:
                    continue
                node = at_path(snapshot, candidate)
                if not isinstance(node, dict):
                    continue
                association = source_record(node, candidate, prepared.observe, budget)
                if association is None or not association.described:
                    continue
                usage = prepared.observe(candidate)
                if association.key == object_key(occurrence) or (
                    not usage.limitation
                    and any(
                        same_contribution(occurrence, item)
                        for item in usage.occurrences
                    )
                ):
                    matches.append(candidate)
            for selected_path in matches:
                if any(
                    child.reference is None
                    and not child.entity
                    and child.path == selected_path
                    and child.model != target
                    for child in bindings
                ):
                    issues.append(
                        Issue(
                            "emission.binding_conflict",
                            "Explicit child binding conflicts with the "
                            "established model relationship",
                            location(selected_path),
                            kind="input_error",
                            expected=target,
                        )
                    )
                selected = ModelUsage(target, selected_path)
                if selected not in bindings:
                    budget.consume("states")
                    bindings.append(selected)
            discovered_sources[key] = tuple(matches)
            return discovered_sources[key]

        def reference_source(path: SourcePath, key: EntityKey) -> bool:
            """Join descriptions only for actual references, not inline source nodes."""
            value = at_path(snapshot, path)
            if (
                isinstance(value, str)
                or isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            ):
                return True
            reference = False
            for candidate in candidate_paths(value, path, budget):
                node = at_path(snapshot, candidate)
                if not isinstance(node, dict):
                    continue
                association = source_record(node, candidate, prepared.observe, budget)
                if association is not None and association.key == key:
                    if association.described:
                        return False
                    reference = True
            return reference

        def check_entity(binding: ModelUsage, model: ModelDeclaration) -> None:
            """Check joined entity facts separately from source-object guarantees."""
            nonlocal checked
            key = entity_for(binding)
            where = (
                f"entity[{binding.reference}]"
                if binding.reference is not None
                else location(binding.path)
            )
            description = prepared.entities.get(key) if key is not None else None
            traits = ValueTraits(
                "node",
                identifier=key.identifier if key is not None else None,
                identifier_kind=(
                    "iri"
                    if key is not None
                    and key.identifier is not None
                    and not key.identifier.startswith("_:")
                    else "blank"
                ),
                types=description.types if description is not None else (),
            )
            checked += 1
            if key is None:
                issues.append(
                    Issue(
                        "emission.node_unverified",
                        "Selected source subject identity could not be established",
                        where,
                        kind="unverified",
                    )
                )
                return
            identity_issues(binding, model, traits, where)
            if description is None or not description.described:
                issues.append(
                    Issue(
                        "emission.reference_missing"
                        if binding.closed
                        else "emission.reference_unverified",
                        "No local description was supplied in this effective graph",
                        where,
                        kind="finding" if binding.closed else "unverified",
                        actual=key.identifier,
                    )
                )
                return
            if set(model.types) - set(description.types):
                issues.append(
                    Issue(
                        "emission.types_mismatch",
                        "Entity lacks required asserted types in this graph",
                        where,
                        expected=", ".join(model.types),
                        actual=", ".join(description.types),
                    )
                )
            for field in model.fields:
                budget.consume("work")
                checked += 1
                facts = []
                for item in description.facts:
                    budget.consume("work")
                    if (
                        item.predicate == field.predicate
                        and item.reverse == field.reverse
                    ):
                        facts.append(item)
                field_where = f"{where}.{field.name}"
                if not facts and field.emission == "guaranteed":
                    issues.append(
                        Issue(
                            "emission.required_entity_fact_missing",
                            "Required predicate is absent from the selected graph",
                            field_where,
                        )
                    )
                for fact in facts:
                    budget.consume("work", 1 + len(field.values))
                    matches = tuple(
                        _matches(value, fact, registry, prepared.entities)
                        for value in field.values
                    )
                    if _corpus is not None:
                        _corpus.branch(
                            binding,
                            model.identifier,
                            field.name,
                            matches,
                            empty=fact.empty,
                        )
                    if True not in matches:
                        issues.append(
                            Issue(
                                "emission.value_unverified"
                                if None in matches
                                else "emission.value_mismatch",
                                "Entity value does not establish a declared branch",
                                field_where,
                                kind="unverified" if None in matches else "finding",
                            )
                        )
                    if fact.traits.kind == "literal":
                        result = _literal_result(
                            fact, field_where, budget, native_literals
                        )
                        checked += result.checked
                        issues.extend(result.issues)
                    budget.consume("work", len(field.values))
                    targets = {
                        value.model
                        for value in field.values
                        if value.model is not None
                        and _matches(value, fact, registry, prepared.entities)
                        is not False
                    }
                    child_key = object_key(fact)
                    if (
                        targets
                        and child_key is not None
                        and not reference_binding(child_key, targets)
                        and not bind_reference(child_key, targets)
                    ):
                        issues.append(
                            Issue(
                                "emission.nested_scope_unverified",
                                "Linked model entity requires a binding in this graph",
                                field_where,
                                kind="unverified",
                            )
                        )

                issues.extend(
                    check_occurrences(
                        field.output,
                        facts,
                        where=field_where,
                        budget=budget,
                        native_literals=native_literals,
                    )
                )

        for binding in bindings:
            budget.consume("work")
            if not isinstance(binding, ModelUsage):
                raise InputError("Expected typed model usage bindings")
            if binding.model not in registry:
                issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "Usage references an unknown model",
                        location(binding.path),
                        kind="input_error",
                        actual=binding.model,
                    )
                )
        expectations = []
        for binding in bindings:
            budget.consume("work")
            model = registry.get(binding.model)
            if model is None:
                continue
            if binding.reference is not None or binding.entity:
                check_entity(binding, model)
                continue
            try:
                node = at_path(snapshot, binding.path)
            except (KeyError, IndexError) as error:
                raise InputError(
                    "No supplied model usage", location=location(binding.path)
                ) from error
            if not isinstance(node, dict):
                raise InputError(
                    "Model bindings select compact node objects",
                    location=location(binding.path),
                )
            checked += 1
            node_traits = traits_for(binding.path)
            if len(node_traits) != 1:
                issues.append(
                    Issue(
                        "emission.node_unverified",
                        "Selected compact node cannot be associated "
                        "with exactly one expanded node",
                        location(binding.path),
                        kind="unverified",
                    )
                )
            elif node_traits[0].kind not in {"node", "iri", "blank"}:
                issues.append(
                    Issue(
                        "emission.node_kind_mismatch",
                        "Selected output is not an RDF node",
                        location(binding.path),
                        actual=node_traits[0].kind,
                    )
                )
            elif set(model.types) - set(node_traits[0].types):
                issues.append(
                    Issue(
                        "emission.types_mismatch",
                        "Emitted node does not assert the declared RDF types",
                        location(binding.path),
                        expected=", ".join(model.types),
                        actual=", ".join(node_traits[0].types),
                    )
                )
            if len(node_traits) == 1:
                identity_issues(binding, model, node_traits[0], location(binding.path))
            selected_fields = [
                (field, (*binding.path, field.key or field.name))
                for field in model.fields
            ]
            if model.coverage is not None:
                by_name = {field.name: field for field in model.fields}
                by_key = {field.key or field.name: field for field in model.fields}
                by_key.update(
                    {key: by_name[name] for key, name in model.coverage.aliases}
                )
                for prop in source_properties(node, binding.path, budget):
                    alias_field = by_key.get(prop.key)
                    if alias_field is not None and prop.path != (
                        *binding.path,
                        alias_field.key or alias_field.name,
                    ):
                        selected_fields.append((alias_field, prop.path))
            aggregate_fields = {
                field.name
                for field, selected_path in selected_fields
                if selected_path != (*binding.path, field.key or field.name)
            }
            for field, path in selected_fields:
                budget.consume("work")
                if field.output is not None and field.name in aggregate_fields:
                    field = replace(
                        field,
                        output=replace(
                            field.output, minimum_values=None, maximum_values=None
                        ),
                    )
                checked += 1
                try:
                    raw_value = at_path(snapshot, path)
                except KeyError:
                    raw_value = ABSENT
                if raw_value is ABSENT:
                    issues.extend(
                        check_occurrences(
                            field.output,
                            (),
                            where=location(path),
                            budget=budget,
                            native_literals=native_literals,
                            present=False,
                        )
                    )
                    if field.emission == "guaranteed":
                        issues.append(
                            Issue(
                                "emission.required_occurrence_missing",
                                "Guaranteed field was omitted from "
                                "the representative output",
                                location(path),
                            )
                        )
                    continue
                expectations.append(
                    ContextExpectation(path, field.predicate, reverse=field.reverse)
                )
                observed = prepared.observe(path)
                if observed.limitation:
                    issues.append(
                        Issue(
                            "emission.value_unverified",
                            observed.limitation,
                            location(path),
                            kind="unverified",
                        )
                    )
                    continue
                if not observed.occurrences:
                    issues.extend(
                        check_occurrences(
                            field.output,
                            (),
                            where=location(path),
                            budget=budget,
                            native_literals=native_literals,
                            present=True,
                            raw=raw_value,
                        )
                    )
                    if field.emission == "guaranteed":
                        issues.append(
                            Issue(
                                "emission.required_occurrence_missing",
                                "Guaranteed field emitted no RDF occurrence",
                                location(path),
                            )
                        )
                    elif (
                        field.output is not None
                        and field.output.allow_empty
                        and (raw_value is not None or field.output.allow_null)
                    ):
                        expectations.pop()
                    else:
                        issues.append(
                            Issue(
                                "emission.value_unverified",
                                "No value survived the selected representative usage",
                                location(path),
                                kind="unverified",
                            )
                        )
                    continue
                minimum_depth = min(item.depth for item in observed.occurrences)
                for index, occurrence in enumerate(observed.occurrences):
                    budget.consume("work")
                    # Nested contributions are checked by their own explicit binding.
                    if occurrence.depth != minimum_depth:
                        continue
                    if not occurrence.empty and occurrence.traits.kind == "literal":
                        result = _literal_result(
                            occurrence,
                            f"{location(path)}[{index}]",
                            budget,
                            native_literals,
                        )
                        checked += result.checked
                        issues.extend(result.issues)
                    matches = tuple(
                        _matches(value, occurrence, registry, prepared.entities)
                        for value in field.values
                    )
                    if _corpus is not None:
                        _corpus.branch(
                            binding,
                            model.identifier,
                            field.name,
                            matches,
                            empty=occurrence.empty,
                        )
                    if True not in matches:
                        issues.append(
                            Issue(
                                "emission.value_unverified"
                                if None in matches
                                else "emission.value_mismatch",
                                "Observed value cannot be verified "
                                "against a supported branch"
                                if None in matches
                                else "Observed value matches none of "
                                "the declared alternatives",
                                f"{location(path)}[{index}]",
                                kind="unverified" if None in matches else "finding",
                                actual=occurrence.traits.kind,
                            )
                        )
                issues.extend(
                    check_occurrences(
                        field.output,
                        tuple(
                            item
                            for item in observed.occurrences
                            if item.depth == minimum_depth
                        ),
                        where=location(path),
                        budget=budget,
                        native_literals=native_literals,
                        present=True,
                        raw=raw_value,
                    )
                )
                covered: set[SourcePath] = set()
                for occurrence in observed.occurrences:
                    budget.consume("work")
                    if occurrence.depth != minimum_depth or occurrence.empty:
                        continue
                    budget.consume("work", len(field.values))
                    targets = {
                        value.model
                        for value in field.values
                        if value.model is not None
                        and _matches(value, occurrence, registry, prepared.entities)
                        is not False
                    }
                    if not targets:
                        continue
                    child_key = object_key(occurrence)
                    discovered = bind_source(path, occurrence, targets)
                    covered.update(discovered)
                    matched = bool(discovered) or (
                        child_key is not None
                        and reference_source(path, child_key)
                        and reference_binding(child_key, targets)
                    )
                    for child in bindings:
                        if matched:
                            break
                        budget.consume("work")
                        if (
                            child.reference is not None
                            or child.entity
                            or child.model not in targets
                            or child.path in covered
                            or child.path[: len(path)] != path
                        ):
                            continue
                        traits = traits_for(child.path)
                        if len(traits) != 1 or traits[0].kind not in {
                            "node",
                            "iri",
                            "blank",
                        }:
                            continue
                        usage = prepared.observe(child.path)
                        budget.consume("work", len(usage.occurrences))
                        if not usage.limitation and any(
                            same_contribution(occurrence, item)
                            for item in usage.occurrences
                        ):
                            covered.add(child.path)
                            matched = True
                            break
                    if (
                        not matched
                        and child_key is not None
                        and reference_source(path, child_key)
                    ):
                        matched = bind_reference(child_key, targets)
                    if not matched:
                        issues.append(
                            Issue(
                                "emission.nested_scope_unverified",
                                "Nested model output needs an established "
                                "source-node binding",
                                location(path),
                                kind="unverified",
                            )
                        )
        for binding in bindings:
            model = registry.get(binding.model)
            if model is None or model.coverage is None:
                continue
            budget.consume("work")
            checked += 1
            if binding.reference is not None or binding.entity:
                key = entity_for(binding)
                description = prepared.entities.get(key) if key is not None else None
                issues.append(
                    Issue(
                        "emission.source_coverage_unverified",
                        "Entity facts do not establish original source-key coverage",
                        f"entity[{binding.reference}]"
                        if binding.reference
                        else location(binding.path),
                        kind="unverified",
                    )
                )
                if description is not None:
                    issues.extend(
                        predicate_issues(
                            model,
                            (
                                (item.predicate, item.reverse)
                                for item in description.facts
                            ),
                            where=f"entity[{description.key.identifier}]",
                            budget=budget,
                        )
                    )
                continue
            node = at_path(snapshot, binding.path)
            if not isinstance(node, dict):
                continue
            owner = entity_for(binding)
            properties = source_properties(node, binding.path, budget)
            checked += len(properties)
            issues.extend(
                source_issues(
                    model,
                    properties,
                    owner,
                    prepared.observe,
                    where=location(binding.path),
                    budget=budget,
                )
            )
            aliases = dict(model.coverage.aliases)
            for field in model.fields:
                if field.output is None:
                    continue
                budget.consume("work", len(properties))
                paths = [
                    prop.path
                    for prop in properties
                    if aliases.get(prop.key) == field.name
                    or prop.key == (field.key or field.name)
                ]
                if not any(
                    path != (*binding.path, field.key or field.name) for path in paths
                ):
                    continue
                facts: list[ObservedOccurrence] = []
                resolved = owner is not None
                for path in paths:
                    usage = prepared.observe(path)
                    if usage.limitation:
                        resolved = False
                    facts.extend(
                        item
                        for item in usage.occurrences
                        if owner is not None
                        and owns(item, owner)
                        and item.predicate == field.predicate
                        and item.reverse == field.reverse
                    )
                if not resolved:
                    issues.append(
                        Issue(
                            "emission.cardinality_unverified",
                            "Shared alias contributions could not be associated "
                            "with one source owner",
                            location(binding.path),
                            kind="unverified",
                        )
                    )
                    continue
                issues.extend(
                    check_occurrences(
                        replace(field.output, require_property=False),
                        tuple(dict.fromkeys(facts)),
                        where=f"{location(binding.path).rstrip('/')}/{field.name}",
                        budget=budget,
                        native_literals=native_literals,
                    )
                )
        for binding in bindings:
            model = registry.get(binding.model)
            if model is None or not (
                model.emission_groups
                or _corpus is not None
                and _corpus.fields(model.identifier)
            ):
                continue
            states: dict[str, bool | None] = {}
            terms: dict[str, tuple[TermKey, ...] | None] = {}
            needed = {name for group in model.emission_groups for name in group.fields}
            needed.update(
                case.condition.field
                for group in model.emission_groups
                for case in group.scenarios
                if case.condition is not None
            )
            if _corpus is not None:
                needed.update(_corpus.fields(model.identifier))
            entity = binding.reference is not None or binding.entity
            key = entity_for(binding) if entity else None
            description = prepared.entities.get(key) if key is not None else None
            node = None if entity else at_path(snapshot, binding.path)
            established = (
                description is not None and description.described
                if entity
                else len(traits_for(binding.path)) == 1
            )
            source_facts: tuple[ObservedOccurrence, ...] | None = None
            source_complete = True
            if established and not entity and isinstance(node, dict):
                owner = entity_for(binding)
                needed_predicates = {
                    field.predicate for field in model.fields if field.name in needed
                }
                gathered: list[ObservedOccurrence] = []
                if owner is not None:
                    for raw_key in node:
                        if raw_key in {"@context", "@included", "@graph"}:
                            continue
                        if (
                            raw_key in {"@id", "@type"}
                            and raw_key not in needed_predicates
                        ):
                            continue
                        usage = prepared.observe((*binding.path, raw_key))
                        if usage.limitation:
                            source_complete = False
                            continue
                        gathered.extend(
                            item
                            for item in usage.occurrences
                            if subject_key(item) == owner
                            or item.predicate == "@id"
                            and item.traits.identifier == owner.identifier
                        )
                    source_facts = tuple(dict.fromkeys(gathered))
            for field in model.fields:
                if field.name not in needed:
                    continue
                budget.consume("work")
                selected_facts: tuple[ObservedOccurrence, ...] | None = None
                if established and entity and description is not None:
                    selected_facts = tuple(
                        item
                        for item in description.facts
                        if item.predicate == field.predicate
                        and item.reverse == field.reverse
                    )
                elif source_facts is not None:
                    selected_facts = tuple(
                        item
                        for item in source_facts
                        if item.predicate == field.predicate
                        and item.reverse == field.reverse
                    )
                if not entity and selected_facts is not None and isinstance(node, dict):
                    source_key = field.key or field.name
                    declared_facts = (
                        prepared.observe((*binding.path, source_key)).occurrences
                        if source_key in node
                        else ()
                    )
                    if model.coverage is not None:
                        declared_facts += tuple(
                            item
                            for prop in source_properties(node, binding.path, budget)
                            if (prop.key, field.name) in model.coverage.aliases
                            for item in prepared.observe(prop.path).occurrences
                        )
                    for fact in selected_facts:
                        if fact in declared_facts:
                            continue
                        budget.consume("work", 1 + len(field.values))
                        field_where = f"{location(binding.path)}.{field.name}"
                        matches = tuple(
                            _matches(value, fact, registry, prepared.entities)
                            for value in field.values
                        )
                        if _corpus is not None:
                            _corpus.branch(
                                binding,
                                model.identifier,
                                field.name,
                                matches,
                                empty=fact.empty,
                            )
                        if True not in matches:
                            issues.append(
                                Issue(
                                    "emission.value_unverified"
                                    if None in matches
                                    else "emission.value_mismatch",
                                    "Aliased RDF contribution does not establish "
                                    "a declared value branch",
                                    field_where,
                                    kind="unverified" if None in matches else "finding",
                                )
                            )
                        if fact.traits.kind == "literal":
                            result = _literal_result(
                                fact, field_where, budget, native_literals
                            )
                            checked += result.checked
                            issues.extend(result.issues)
                        targets = {
                            value.model
                            for value in field.values
                            if value.model is not None
                            and _matches(value, fact, registry, prepared.entities)
                            is not False
                        }
                        matched_child = False
                        for child in bindings if targets else ():
                            budget.consume("work")
                            if (
                                child.model not in targets
                                or child.reference is not None
                                or child.entity
                                or child.path[: len(binding.path)] != binding.path
                            ):
                                continue
                            child_usage = prepared.observe(child.path)
                            budget.consume("work", len(child_usage.occurrences))
                            if not child_usage.limitation and any(
                                same_contribution(fact, item)
                                for item in child_usage.occurrences
                            ):
                                matched_child = True
                                break
                        if targets and not matched_child:
                            issues.append(
                                Issue(
                                    "emission.nested_scope_unverified",
                                    "Aliased child descriptions require an "
                                    "established model binding",
                                    field_where,
                                    kind="unverified",
                                )
                            )
                states[field.name] = (
                    None
                    if selected_facts is None
                    or not selected_facts
                    and not source_complete
                    else bool(selected_facts)
                )
                terms[field.name] = (
                    None
                    if selected_facts is None or not source_complete
                    else _group_terms(selected_facts, native_literals, budget)
                )
            if _corpus is not None:
                _corpus.scope(binding, states, terms)
            checked += len(model.emission_groups)
            issues.extend(
                check_groups(
                    model,
                    states,
                    terms,
                    _corpus=_corpus,
                    where=f"entity[{binding.reference}]"
                    if binding.reference is not None
                    else location(binding.path),
                    budget=budget,
                )
            )
        mapping = prepared.check(expectations, allow_empty=True)
        checked += mapping.checked
        issues.extend(mapping.issues)
        complete = mapping.complete
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return Report(
        "model-emission",
        checked,
        tuple(issues),
        complete=complete,
        families=("representative-serialization", "literal-values"),
        requested=max(checked, len(usages)),
    )
