"""Closed application-field coverage over proven source and RDF owner scopes."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from rdflib.namespace import RDF

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.jsonld.entities import EntityKey, subject_key
from bijux_ontology.jsonld.observations import (
    ObservedOccurrence,
    ObservedUsage,
    SourcePath,
    location,
)
from bijux_ontology.limits import Budget
from bijux_ontology.reports import Issue


@dataclass(frozen=True)
class SourceProperty:
    """One actual property path, including properties inside canonical @reverse."""

    path: SourcePath
    key: str


def source_properties(
    node: dict[str, object], path: SourcePath, budget: Budget
) -> tuple[SourceProperty, ...]:
    """Traverse canonical reverse properties; keep contexts and literal data opaque."""
    result = []
    for key, raw in node.items():
        budget.consume("work")
        if key == "@reverse" and isinstance(raw, dict):
            for inner in raw:
                budget.consume("work")
                result.append(SourceProperty((*path, key, inner), inner))
        else:
            result.append(SourceProperty((*path, key), key))
    return tuple(result)


def owns(item: ObservedOccurrence, owner: EntityKey) -> bool:
    """Keep original expansion graph and blank identity, including subject metadata."""
    if item.predicate == "@id":
        return (
            EntityKey(
                item.traits.identifier,
                graph=item.graph,
                anonymous_graph=item.anonymous_graph,
                graph_identity=item.graph_identity,
            )
            == owner
        )
    return subject_key(item) == owner


def predicate_issues(
    model: ModelDeclaration,
    predicates: Iterable[tuple[str, bool]],
    *,
    where: str,
    budget: Budget,
) -> tuple[Issue, ...]:
    """Check directed RDF facts; explicit extensions remain visible policy decisions."""
    policy = model.coverage
    if policy is None:
        return ()
    declared = {(field.predicate, field.reverse) for field in model.fields}
    if "@type" in policy.metadata:
        declared.add(("@type", False))
    permitted = {(name, False) for name in policy.extensions} | {
        (name, True) for name in policy.reverse_extensions
    }
    result = []
    seen = set()
    for predicate, reverse in predicates:
        budget.consume("work")
        if predicate == "@id":
            continue
        normalized = "@type" if predicate == str(RDF.type) else predicate
        key = (normalized, reverse)
        if key in seen:
            continue
        seen.add(key)
        if (
            key in declared
            or (str(RDF.type), reverse) in declared
            and normalized == "@type"
        ):
            continue
        allowed = (
            (predicate, reverse) in permitted
            or (str(RDF.type), reverse) in permitted
            and normalized == "@type"
        )
        result.append(
            Issue(
                "emission.extension_allowed"
                if allowed
                else "emission.predicate_undeclared",
                "Explicit extension predicate has no declared value contract"
                if allowed
                else "Emitted predicate has no application field declaration",
                where,
                severity="info" if allowed else "error",
                actual=("reverse " if reverse else "forward ") + predicate,
            )
        )
    return tuple(result)


def source_issues(
    model: ModelDeclaration,
    properties: tuple[SourceProperty, ...],
    owner: EntityKey | None,
    observe: Callable[[SourcePath], ObservedUsage],
    *,
    where: str,
    budget: Budget,
) -> tuple[Issue, ...]:
    """Close actual source keys without inventing correspondence for lost values."""
    policy = model.coverage
    if policy is None:
        return ()
    authorized = {field.key or field.name for field in model.fields} | {
        key for key, _ in policy.aliases
    }
    extensions = {(name, False) for name in policy.extensions} | {
        (name, True) for name in policy.reverse_extensions
    }
    result = []
    facts: list[ObservedOccurrence] = []
    for prop in properties:
        budget.consume("work")
        if prop.key in {"@context", "@index"} and prop.key in policy.metadata:
            continue
        if prop.key in {"@graph", "@included", "@context", "@index"}:
            result.append(
                Issue(
                    "emission.source_field_undeclared",
                    "Source structure is outside the closed field contract",
                    location(prop.path),
                    actual=prop.key,
                )
            )
            continue
        usage = observe(prop.path)
        own = tuple(
            item
            for item in usage.occurrences
            if owner is not None and owns(item, owner)
        )
        facts.extend(own)
        if usage.limitation or owner is None:
            result.append(
                Issue(
                    "emission.source_coverage_unverified",
                    usage.limitation or "Source owner could not be established",
                    location(prop.path),
                    kind="unverified",
                )
            )
            continue
        if (
            prop.key in authorized
            or prop.key in policy.metadata
            and prop.key in {"@id", "@type"}
        ):
            continue
        if not usage.occurrences and "@index" in policy.metadata:
            result.append(
                Issue(
                    "emission.source_coverage_unverified",
                    "Non-RDF metadata aliases lack an established usage witness",
                    location(prop.path),
                    kind="unverified",
                )
            )
            continue
        metadata = bool(own) and all(
            item.predicate in policy.metadata and item.predicate in {"@id", "@type"}
            for item in own
        )
        extended = bool(own) and all(
            (item.predicate, item.reverse) in extensions for item in own
        )
        if not metadata and not extended:
            result.append(
                Issue(
                    "emission.source_field_undeclared",
                    "Source property has no declaration or proven explicit extension",
                    location(prop.path),
                    actual=prop.key,
                )
            )
    result.extend(
        predicate_issues(
            model,
            ((item.predicate, item.reverse) for item in facts),
            where=where,
            budget=budget,
        )
    )
    return tuple(result)
