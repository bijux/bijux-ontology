"""Application-owned vocabulary references and observed JSON-LD occurrences."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from rdflib import URIRef

from bijux_ontology._jsonpaths import SourcePath, location
from bijux_ontology.data.query_usage.types import QueryUsage
from bijux_ontology.declarations.types import ModelDeclaration, TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.jsonld.observations import ObservedOccurrence, ObservedUsage
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.models.resource_roles.types import RoleUse
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_node

UseRole = Literal["term", "class", "predicate", "datatype", "concept", "individual"]


@dataclass(frozen=True)
class Use:
    """One actual reference; graph identity tokens are local to its preparation."""

    iri: str
    role: UseRole
    location: str
    model: str | None = None
    field: str | None = None
    branch: int | None = None
    path: SourcePath | None = None
    reverse: bool = False
    container: str | None = None
    graph: str | None = None
    anonymous_graph: bool = False
    graph_identity: int | None = None


@dataclass(frozen=True)
class UseIndex:
    """Finite references; incomplete extraction never certifies unused terms."""

    entries: tuple[Use, ...]
    report: Report

    def uses(self, iri: str) -> tuple[Use, ...]:
        """Return actual locations for the exact IRI, without prefix rewriting."""
        absolute_iri(iri)
        return tuple(item for item in self.entries if item.iri == iri)


def _observed(
    iri: str,
    role: UseRole,
    usage: ObservedUsage,
    item: ObservedOccurrence,
) -> Use:
    """Keep the actual selected edge direction, graph and path as evidence."""
    return Use(
        iri,
        role,
        item.location or location(usage.path),
        path=usage.path,
        reverse=item.reverse,
        container=item.container,
        graph=item.graph,
        anonymous_graph=item.anonymous_graph,
        graph_identity=item.graph_identity,
    )


def index_uses(
    terms: Sequence[TermDeclaration] = (),
    models: Sequence[ModelDeclaration] = (),
    *,
    observed_usages: Sequence[ObservedUsage] = (),
    role_uses: Sequence[RoleUse] = (),
    query_usages: Sequence[QueryUsage] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> UseIndex:
    """Index supplied model contracts and processor-confirmed context meanings.

    Instance identifiers and graph names are occurrence evidence, not vocabulary
    declarations. Child-model RDF types are indexed at their referring field;
    resolving a child never recursively walks the model registry.
    """
    if any(
        not isinstance(values, Sequence) or isinstance(values, (str, bytes))
        for values in (terms, models, observed_usages, role_uses, query_usages)
    ):
        raise InputError("Use inputs must be sequences of typed declarations or usages")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    entries: list[Use] = []
    seen: set[Use] = set()
    checked = 0
    complete = True
    children: list[Report] = []

    def retain(use: Use) -> None:
        """Validate and bound each distinct actual reference before retaining it."""
        absolute_iri(use.iri, location=use.location)
        operation.consume("work")
        if use not in seen:
            operation.consume("states")
            entries.append(use)
            seen.add(use)

    try:
        registry: dict[str, ModelDeclaration] = {}
        for model in models:
            operation.consume("work")
            if not isinstance(model, ModelDeclaration):
                raise InputError("Use indexing requires typed model declarations")
            if model.identifier in registry and registry[model.identifier] != model:
                raise InputError(
                    "Conflicting model identifier", location=model.identifier
                )
            registry[model.identifier] = model
        for term in terms:
            operation.consume("work")
            if not isinstance(term, TermDeclaration):
                raise InputError("Use indexing requires typed term declarations")
            retain(Use(term.iri, "term", term.location))
            checked += 1
        for query_usage in query_usages:
            operation.consume("work")
            if type(query_usage) is not QueryUsage:
                raise InputError("Query use indexing requires actual parsed QueryUsage")
            retain_report(query_usage.report, operation, issues)
            children.append(query_usage.report)
            complete = complete and query_usage.report.complete
            for reference in query_usage.references:
                operation.consume("work")
                if reference.role not in ("class", "predicate", "datatype"):
                    continue
                bound_text(reference.iri, operation)
                bound_text(reference.location, operation)
                operation.consume("bytes", 192)
                retain(
                    Use(
                        reference.iri,
                        reference.role,
                        reference.location,
                        reverse=reference.reverse,
                    )
                )
            checked += 1
        for use in role_uses:
            operation.consume("work")
            if not isinstance(use, RoleUse):
                raise InputError("Use indexing requires typed resource-role uses")
            bound_node(use.term, operation)
            bound_text(use.location, operation)
            if not isinstance(use.term, URIRef):
                issues.append(
                    Issue(
                        "upgrade.usage_unavailable",
                        "Anonymous role references need established owner attribution",
                        use.location,
                        kind="unverified",
                    )
                )
                continue
            chosen = (
                use.contract.bridge.source_role
                if use.contract.bridge is not None
                else use.contract.kind
            )
            role: UseRole = "individual" if chosen == "instance" else chosen
            retain(Use(str(use.term), role, use.location))
            for iri in use.contract.types:
                bound_text(iri, operation)
                retain(Use(iri, "class", use.location))
            if use.contract.bridge is not None:
                bound_text(use.contract.bridge.predicate, operation)
                retain(Use(use.contract.bridge.predicate, "predicate", use.location))
            checked += 1
        for model in registry.values():
            operation.consume("work")
            where = model.location or model.identifier
            for iri in model.types:
                retain(Use(iri, "class", where, model.identifier))
            for field in model.fields:
                operation.consume("work")
                where = field.location or f"{model.identifier}.{field.name}"
                if field.predicate not in ("@id", "@type"):
                    retain(
                        Use(
                            field.predicate,
                            "predicate",
                            where,
                            model.identifier,
                            field.name,
                            reverse=field.reverse,
                        )
                    )
                for index, branch in enumerate(field.values):
                    operation.consume("work")
                    for iri in branch.types:
                        retain(
                            Use(
                                iri,
                                "class",
                                where,
                                model.identifier,
                                field.name,
                                index,
                                reverse=field.reverse,
                                container=branch.container,
                            )
                        )
                    if branch.datatype is not None:
                        retain(
                            Use(
                                branch.datatype,
                                "datatype",
                                where,
                                model.identifier,
                                field.name,
                                index,
                                reverse=field.reverse,
                                container=branch.container,
                            )
                        )
                    if branch.model is not None:
                        child = registry.get(branch.model)
                        if child is None:
                            issues.append(
                                Issue(
                                    "upgrade.child_model_unavailable",
                                    "Referenced child model was not supplied",
                                    where,
                                    kind="unverified",
                                    actual=branch.model,
                                )
                            )
                        else:
                            for iri in child.types:
                                retain(
                                    Use(
                                        iri,
                                        "class",
                                        where,
                                        model.identifier,
                                        field.name,
                                        index,
                                        reverse=field.reverse,
                                        container=branch.container,
                                    )
                                )
            checked += 1
        for usage in observed_usages:
            operation.consume("work")
            if not isinstance(usage, ObservedUsage):
                raise InputError("Use indexing requires actual observed usages")
            checked += 1
            if (
                usage.limitation
                or usage.dropped
                or usage.empty
                or not usage.occurrences
            ):
                issues.append(
                    Issue(
                        "upgrade.usage_unavailable",
                        usage.limitation or "Selected usage has no established meaning",
                        location(usage.path),
                        kind="unverified",
                    )
                )
                continue
            for item in usage.occurrences:
                operation.consume("work")
                where = item.location or location(usage.path)
                traits = item.traits

                if item.predicate == "@type":
                    if (
                        traits.identifier_kind == "iri"
                        and traits.identifier is not None
                    ):
                        retain(_observed(traits.identifier, "class", usage, item))
                    else:
                        issues.append(
                            Issue(
                                "upgrade.usage_unavailable",
                                "Selected class reference has no absolute IRI",
                                where,
                                kind="unverified",
                            )
                        )
                elif item.predicate != "@id":
                    retain(_observed(item.predicate, "predicate", usage, item))
                if traits.datatype is not None:
                    retain(_observed(traits.datatype, "datatype", usage, item))
                for iri in traits.types:
                    retain(_observed(iri, "class", usage, item))
    except LimitExceeded as error:
        issues.exhausted(str(error), location="upgrade.uses")
        complete = False
    return UseIndex(
        tuple(entries),
        Report(
            "upgrade.uses",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            children=tuple(children),
            families=("application.uses",),
            regime="supplied-application-references",
        ),
    )
