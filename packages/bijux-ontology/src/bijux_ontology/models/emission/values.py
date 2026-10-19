"""Observed value matching and exact emitted literal qualification."""

from __future__ import annotations

from dataclasses import replace

from rdflib import Literal, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.types import ModelDeclaration, ValueDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.jsonld.evidence.entities import EntityInventory, object_key
from bijux_ontology.jsonld.evidence.occurrences import ObservedOccurrence
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.models.emission.application import application_matches
from bijux_ontology.models.emission.direction import DirectionCheck
from bijux_ontology.models.emission.matching import matches_value
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import literal_term


def _matches(
    value: ValueDeclaration,
    occurrence: ObservedOccurrence,
    registry: dict[str, ModelDeclaration],
    inventory: EntityInventory | None = None,
    rdf_direction: DirectionPolicy | None = None,
    *,
    budget: Budget,
    native_literals: dict[str, Literal],
    lexical_valid: bool | None = None,
    role_checks: _RoleChecks | None = None,
) -> bool | None:
    """Resolve actual joined descriptions before shared scalar contract matching."""
    traits = occurrence.traits
    if (
        inventory is not None
        and value.kind in {"iri", "object"}
        and (traits.kind in {"iri", "blank", "node"})
    ):
        key = object_key(occurrence)
        description = inventory.get(key) if key is not None else None
        if description is not None and description.described:
            traits = replace(traits, types=description.types)
    if value.kind == "literal" and traits.direction is not None:
        if rdf_direction == "compound-literal" and value.datatype is not None:
            return False
        if rdf_direction == "i18n-datatype":
            traits = replace(
                traits,
                datatype="https://www.w3.org/ns/i18n#"
                + (traits.language or "").lower()
                + "_"
                + traits.direction,
            )
    role_result = None
    if value.role is not None and role_checks is not None:
        role_term = None
        if traits.identifier_kind == "iri" and traits.identifier is not None:
            bound_text(traits.identifier, budget)
            role_term = URIRef(traits.identifier)
        role_result = role_checks.matches(
            value.role,
            role_term,
            traits.types,
            value.types,
            predicate=occurrence.predicate,
            reverse=occurrence.reverse,
        )
    matched = matches_value(
        value,
        traits,
        registry,
        container=occurrence.container,
        empty=occurrence.empty,
        role_result=role_result,
    )
    if matched is False or value.constraints is None or occurrence.empty:
        return matched
    term: Node | None = None
    if occurrence.traits.direction is None:
        if traits.identifier_kind == "iri" and traits.identifier is not None:
            term = URIRef(traits.identifier)
        elif traits.kind == "literal":
            raw = occurrence.value
            if isinstance(raw, dict) and isinstance(raw.get("@value"), str):
                term = literal_term(
                    raw["@value"], datatype=traits.datatype, language=traits.language
                )
            else:
                term = native_literals.get(occurrence.value_json)
    application = application_matches(
        value.constraints, term, budget=budget, lexical_valid=lexical_valid
    )
    return (
        False
        if application is False
        else None
        if matched is None or application is None
        else True
    )


def _literal_result(
    occurrence: ObservedOccurrence,
    where: str,
    budget: Budget,
    native: dict[str, Literal],
    direction_check: DirectionCheck | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Validate actual lexical values, obtaining native spelling from PyLD itself."""
    if direction_check is not None:
        direction_check.observe(occurrence.traits)
    raw = occurrence.value
    if not isinstance(raw, dict) or "@value" not in raw:
        raise RuntimeError("Observed literal lacks its expanded value")
    value = raw["@value"]
    datatype, language = (occurrence.traits.datatype, occurrence.traits.language)
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
    return check_literal(
        term, location=where, datatype_registry=datatype_registry, budget=budget
    )
