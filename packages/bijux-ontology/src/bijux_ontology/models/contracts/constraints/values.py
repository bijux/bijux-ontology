"""Declared value representation and independent branch constraints."""

from dataclasses import replace

from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.absence import ABSENT
from bijux_ontology.declarations.types import (
    FieldDeclaration,
    ModelDeclaration,
    TermDeclaration,
    ValueDeclaration,
)
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.owl.relations.classes import ClassContracts
from bijux_ontology.owl.relations.properties import PropertyContract
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.literals.checking import (
    Regime,
)
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .classes import class_issues
from .datatypes import datatype_issues


def value_contract_issues(value: ValueDeclaration, location: str) -> list[Issue]:
    """Identify unsupported value contracts without skipping other branches."""
    issues: list[Issue] = []
    if value.kind == "unknown":
        issues.append(
            Issue(
                "models.value_unverified",
                "Unknown serialisation cannot establish RDF value kind",
                location,
                kind="unverified",
            )
        )
    if value.container not in (None, "@set", "@list"):
        issues.append(
            Issue(
                "models.container_unverified",
                "Container requires a representative JSON-LD usage",
                location,
                kind="unverified",
                actual=value.container,
            )
        )
    return issues


def branch_issues(
    field: FieldDeclaration,
    value: ValueDeclaration,
    registry: dict[str, ModelDeclaration],
    classes: ClassContracts,
    location: str,
    vocabulary: Vocabulary,
    contract: PropertyContract,
    regime: Regime,
    budget: Budget,
    *,
    rdf_direction: DirectionPolicy | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> list[Issue]:
    """Check one original branch while keeping list heads and items distinct."""
    issues: list[Issue] = []
    if (
        value.role is not None
        and value.role.kind == "instance"
        and value.role.bridge is None
        and value.role.types
    ):
        value = replace(
            value, types=tuple(dict.fromkeys((*value.types, *value.role.types)))
        )
    if (
        value.model is not None
        and value.model not in registry
        and value.container != "@list"
    ):
        return issues
    if value.container not in (None, "@set", "@list"):
        return issues
    if value.container == "@list":
        if contract.literal_value:
            issues.append(
                Issue(
                    "models.list_head_kind_conflict",
                    "An RDF list head is a resource, not a data-property literal",
                    f"{location}.head",
                )
            )
        issues.extend(
            class_issues(
                (str(RDF.List),),
                contract.values,
                classes,
                f"{location}.head",
                "List head",
                budget,
            )
        )
        issues.extend(
            branch_issues(
                replace(field, reverse=False),
                replace(value, container=None),
                registry,
                classes,
                f"{location}.items",
                vocabulary,
                PropertyContract((), ()),
                regime,
                budget,
                rdf_direction=rdf_direction,
                datatype_registry=datatype_registry,
            )
        )
        return issues
    if value.kind == "unknown":
        return issues
    if (
        value.kind == "literal"
        and value.direction is None
        and value.datatype is None
        and rdf_direction is not None
    ):
        if rdf_direction == "compound-literal":
            if (
                field.reverse
                or contract.object_property
                or contract.literal_value
                or contract.values
            ):
                return [
                    Issue(
                        "models.direction_unverified",
                        "Unchecked direction permits either a physical literal "
                        "or a compound resource",
                        location,
                        kind="unverified",
                    )
                ]
            return issues
        # The literal kind is stable under i18n encoding, but its datatype is
        # conditional on direction. A language tag cannot promise langString.
        value = replace(value, language=None)
    encoded = isinstance(value.direction, str)
    declared_datatype = value.datatype
    if encoded:
        if rdf_direction is None:
            return [
                Issue(
                    "models.direction_unverified",
                    "Directional output requires an explicit RDF representation policy",
                    location,
                    kind="unverified",
                )
            ]
        if rdf_direction == "compound-literal":
            if value.datatype is not None:
                issues.append(
                    Issue(
                        "models.value_kind_conflict",
                        "A compound literal is a physical RDF resource, "
                        "not a typed literal",
                        location,
                    )
                )
            value = ValueDeclaration("object")
        else:
            language = value.language
            datatype = (
                "https://www.w3.org/ns/i18n#"
                + (language.lower() if isinstance(language, str) else "")
                + "_"
                + str(value.direction)
                if language is ABSENT or isinstance(language, str)
                else None
            )
            if value.datatype is not None and (
                datatype is not None
                and value.datatype != datatype
                or not value.datatype.startswith("https://www.w3.org/ns/i18n#")
                or not value.datatype.endswith("_" + str(value.direction))
            ):
                issues.append(
                    Issue(
                        "models.datatype_conflict",
                        "Explicit physical datatype differs from the "
                        "selected direction encoding",
                        location,
                        expected=value.datatype,
                        actual=datatype,
                    )
                )
            value = ValueDeclaration("literal", datatype=datatype or value.datatype)
    child = registry.get(value.model) if value.model else None
    types = tuple(dict.fromkeys((*value.types, *(child.types if child else ()))))
    if value.types:
        issues.extend(
            check_vocabulary(
                vocabulary,
                [
                    TermDeclaration(iri, (str(OWL.Class), str(RDFS.Class)), location)
                    for iri in value.types
                ],
                regime="owl-dl" if regime == "owl-dl" else "rdf",
                budget=budget,
            ).issues
        )
    literal = value.kind == "literal"
    if field.reverse and literal:
        issues.append(
            Issue(
                "models.reverse_literal_conflict",
                "A reversed property cannot use a literal as its subject",
                location,
            )
        )
        return issues
    if (literal and contract.object_property) or (
        not literal and contract.literal_value
    ):
        issues.append(
            Issue(
                "models.value_kind_conflict",
                "Serialised value kind conflicts with the property declaration",
                location,
                actual=value.kind,
            )
        )
    constraints = contract.values
    if literal:
        datatype = (
            str(RDF.langString) if isinstance(value.language, str) else value.datatype
        )
        checked_datatype = declared_datatype if encoded else datatype
        if checked_datatype:
            issues.extend(
                check_vocabulary(
                    vocabulary,
                    [
                        TermDeclaration(
                            checked_datatype, (str(RDFS.Datatype),), location
                        )
                    ],
                    regime="owl-dl" if regime == "owl-dl" else "rdf",
                    budget=budget,
                ).issues
            )
        issues.extend(
            datatype_issues(
                value,
                constraints,
                location,
                classes,
                regime,
                budget,
                encoded=encoded and declared_datatype is None,
                datatype_registry=datatype_registry,
            )
        )
    else:
        issues.extend(
            class_issues(types, constraints, classes, location, "Value", budget)
        )
    return issues
