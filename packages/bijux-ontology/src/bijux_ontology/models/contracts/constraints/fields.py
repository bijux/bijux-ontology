"""Field constraints composed over one shared model and ontology inventory."""

from dataclasses import replace

from rdflib import URIRef
from rdflib.namespace import RDF

from bijux_ontology.declarations.types import (
    ModelDeclaration,
    TermDeclaration,
)
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.models.contracts.groups import declaration_states
from bijux_ontology.models.contracts.propagation import ModelContracts
from bijux_ontology.owl.relations.classes import ClassContracts
from bijux_ontology.rdf.literals.checking import (
    Regime,
)
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .classes import class_issues
from .values import branch_issues


def field_issues(
    model: ModelDeclaration,
    registry: dict[str, ModelDeclaration],
    classes: ClassContracts,
    vocabulary: Vocabulary,
    regime: Regime,
    contracts: ModelContracts,
    *,
    rdf_direction: DirectionPolicy | None = None,
) -> tuple[int, list[Issue]]:
    """Compose field obligations using the shared model and ontology state."""
    if model.emission_groups:
        states = declaration_states(model, contracts.budget)
        governed = {name for group in model.emission_groups for name in group.fields}
        group_issues: list[Issue] = []
        checked = 0
        for state in states:
            promised = set(contracts.guaranteed_types(model))
            for field in model.fields:
                if field.name in state.promised and not field.predicate.startswith("@"):
                    promised.update(
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    )
            for field in model.fields:
                if field.name in governed and field.name not in state.present:
                    continue
                parent_types = set(promised)
                if field.name in governed and not field.predicate.startswith("@"):
                    parent_types.update(
                        str(node)
                        for node in contracts.property(field).parents
                        if isinstance(node, URIRef)
                    )
                count, findings = field_issues(
                    replace(
                        model,
                        types=tuple(sorted(parent_types)),
                        fields=(field,),
                        emission_groups=(),
                        coverage=None,
                    ),
                    registry,
                    classes,
                    vocabulary,
                    regime,
                    contracts,
                    rdf_direction=rdf_direction,
                )
                checked += count
                group_issues.extend(findings)
        return checked, list(dict.fromkeys(group_issues))
    issues: list[Issue] = []
    checked = 0
    for field in model.fields:
        contracts.budget.consume("work")
        location = field.location or f"{model.identifier}.{field.name}"
        checked += 1 + len(field.values)
        if field.predicate.startswith("@"):
            issues.append(
                Issue(
                    "models.keyword_unverified",
                    "Keyword semantics require a representative JSON-LD usage",
                    location,
                    kind="unverified",
                )
            )
            continue
        issues.extend(
            check_vocabulary(
                vocabulary,
                [TermDeclaration(field.predicate, (str(RDF.Property),), location)],
                regime="owl-dl" if regime == "owl-dl" else "rdf",
                budget=contracts.budget,
            ).issues
        )
        contract = contracts.property(field)
        constraints = contract.parents
        if contract.unsupported:
            issues.append(
                Issue(
                    "models.property_expression_unverified",
                    "Relevant property relations require unsupported expressions",
                    location,
                    kind="unverified",
                )
            )
        if contract.bottom:
            issues.append(
                Issue(
                    "models.bottom_property_conflict",
                    "An emitted occurrence cannot use an OWL bottom property",
                    location,
                )
            )
        if contract.literal_parent:
            issues.append(
                Issue(
                    "models.parent_kind_conflict",
                    "A resource model cannot be the literal endpoint "
                    "of a data property",
                    location,
                )
            )
        issues.extend(
            class_issues(
                model.types,
                constraints,
                classes,
                location,
                "Parent",
                contracts.budget,
            )
        )
        if not field.values:
            issues.append(
                Issue(
                    "models.value_unverified",
                    "Field has no known serialised value branches",
                    location,
                    kind="unverified",
                )
            )
        for index, branch in enumerate(field.values):
            issues.extend(
                branch_issues(
                    field,
                    branch,
                    registry,
                    classes,
                    f"{location}[{index}]",
                    vocabulary,
                    contract,
                    regime,
                    contracts.budget,
                    rdf_direction=rdf_direction,
                    datatype_registry=contracts.datatype_registry,
                )
            )
    return checked, issues
