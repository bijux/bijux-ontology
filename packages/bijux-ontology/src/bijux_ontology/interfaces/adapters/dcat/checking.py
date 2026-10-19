"""Actual selected catalog records, reference coverage and maintained SHACL checks."""

import time
from dataclasses import dataclass
from threading import Event

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import DCTERMS, FOAF, RDF, SH

from bijux_ontology.data.projection.reading import project_records
from bijux_ontology.data.projection.types import ReadBinding, ReadField
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.coverage import ShaclCoverageContract

from .compiling import _DEFAULT_PROFILE, DCAT, prepare_catalog_profile, shape_iri
from .types import (
    DCAT_PROFILE,
    ROLES,
    CatalogBinding,
    CatalogRole,
    Dcat3Profile,
    PreparedCatalogProfile,
)


@dataclass(frozen=True)
class CatalogCheck:
    """Original owned catalog and explicit application-profile evaluation evidence."""

    source: PreparedSource | None
    profile: PreparedCatalogProfile | None
    report: Report


def _references(
    graph: Graph,
    bindings: tuple[CatalogBinding, ...],
    policy: Dcat3Profile,
    operation: Operation,
    issues: IssueCollector,
) -> None:
    """Require selected related records unless their external service is permitted."""
    known = {(binding.subject, binding.role) for binding in bindings}
    versions = (DCAT.previousVersion, DCAT.hasVersion, DCAT.hasCurrentVersion)
    for binding in bindings:
        operation.consume("work")
        role = binding.role
        links: list[tuple[URIRef, tuple[CatalogRole, ...]]] = []
        if role == "catalog":
            links += [(DCAT.dataset, ("dataset", "series")), (DCAT.record, ("record",))]
        elif role == "dataset":
            links += [
                (DCAT.distribution, ("distribution",)),
                (DCAT.inSeries, ("series",)),
            ]
        elif role == "series":
            links += [
                (predicate, ("dataset",)) for predicate in (DCAT.first, DCAT.last)
            ]
        elif role == "distribution":
            links += [(DCAT.accessService, ("service",))]
        elif role == "service":
            links += [(DCAT.servesDataset, ("dataset", "series"))]
        elif role == "record":
            links += [(FOAF.primaryTopic, ("dataset", "series"))]
        if role in ("catalog", "dataset", "series", "distribution", "service"):
            targets: tuple[CatalogRole, ...] = (
                ("dataset", "series") if role in ("dataset", "series") else (role,)
            )
            links += [(predicate, targets) for predicate in versions]
            links += [(DCTERMS.temporal, ("period",))]
            for predicate, allowed in (
                (DCAT.theme, policy.themes),
                (DCTERMS.license, policy.licenses),
            ):
                if allowed is not None:
                    for value in graph.objects(binding.subject, predicate):
                        bound_node(value, operation)
                        operation.consume("work", len(allowed.excluded) + 1)
                        if value in allowed.excluded:
                            issues.append(
                                Issue(
                                    "catalog.controlled_forbidden",
                                    "Controlled vocabulary excludes this value",
                                    str(binding.subject),
                                    actual=str(value),
                                )
                            )
        for predicate, roles in links:
            for target in graph.objects(binding.subject, predicate):
                bound_node(target, operation)
                operation.consume(
                    "work", len(roles) + len(policy.external_services) + 1
                )
                if (
                    predicate == DCAT.accessService
                    and target in policy.external_services
                ):
                    continue
                if not any((target, expected) in known for expected in roles):
                    issues.append(
                        Issue(
                            "catalog.reference_unverified",
                            "Related record is outside the selected profile population",
                            str(binding.subject),
                            kind="unverified",
                            expected=str(predicate),
                            actual=str(target),
                        )
                    )
        if role == "series":
            for member in graph.subjects(DCAT.inSeries, binding.subject):
                bound_node(member, operation)
                operation.consume("work")
                if (member, "dataset") not in known:
                    issues.append(
                        Issue(
                            "catalog.reference_unverified",
                            "Series member is outside the selected dataset population",
                            str(binding.subject),
                            kind="unverified",
                            actual=str(member),
                        )
                    )


def check_catalog(
    source: PreparedSource | Graph,
    bindings: tuple[CatalogBinding, ...],
    *,
    profile: Dcat3Profile | PreparedCatalogProfile = _DEFAULT_PROFILE,
    graph_name: GraphSelection | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> CatalogCheck:
    """Validate only the supplied catalog population under explicit profile choices.

    Each selected role is checked for its asserted type and profile fields. Linked
    records require explicit selection, except named permitted external services.
    Series membership uses actual inSeries edges. Themes/licenses have only the
    supplied vocabulary scope. Missing downloadURL and shared distributions are
    not errors by themselves. Dates/version links are checked when supplied;
    no temporal ordering, OWL consistency or complete DCAT ontology claim is made.
    """
    if type(bindings) is not tuple or any(
        type(binding) is not CatalogBinding for binding in bindings
    ):
        raise InputError("Catalog checks require finite CatalogBinding tuples")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    prepared = None
    qualified = None
    checked = 0
    complete = False
    try:
        operation.consume("states", len(bindings) * 8 + 16)
        operation.consume("bytes", 256 * len(bindings) + 1024)
        if not bindings or not any(binding.role == "catalog" for binding in bindings):
            raise InputError("Select a nonempty population including a catalog")
        seen = set()
        for binding in bindings:
            bound_node(binding.subject, operation)
            key = (binding.subject, binding.role)
            if key in seen:
                raise InputError("Catalog role selections must be distinct")
            seen.add(key)
        qualified = (
            profile
            if isinstance(profile, PreparedCatalogProfile)
            else prepare_catalog_profile(profile, budget=operation)
        )
        children.append(qualified.report)
        retain_report(qualified.report, operation, issues)
        raw = (ReadField("type", RDF.type, mode="many", minimum=0),)
        projection = project_records(
            source,
            tuple(
                ReadBinding(str(i), binding.subject, raw)
                for i, binding in enumerate(bindings)
            ),
            graph_name=graph_name,
            budget=operation,
        )
        children.append(projection.report)
        retain_report(projection.report, operation, issues)
        if projection.report.verified and len(projection.records) == len(bindings):
            reference = projection.records[0].reference
            prepared = reference.source
            graph = prepared._read(reference.graph, budget=operation)
            _references(graph, bindings, qualified.policy, operation, issues)

            def evaluate(
                shapes: PreparedSource,
                focus: tuple[URIRef | BNode, ...],
                roots: tuple[URIRef, ...],
                coverage: ShaclCoverageContract,
                mode: str,
            ) -> bool:
                """Adopt each completed native result before the next fallible stage."""
                remaining = operation.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded("Catalog validation exceeded its deadline")
                result = validate_shapes(
                    prepared,
                    shapes,
                    graph_name=reference.graph,
                    mode=mode,
                    focus_nodes=focus,
                    use_shapes=roots,
                    preflight=True,
                    coverage=coverage,
                    budget=operation,
                    timeout=remaining,
                )
                children.append(result.report)
                retain_report(result.report, operation, issues)
                return result.report.complete

            named = tuple(
                binding for binding in bindings if isinstance(binding.subject, URIRef)
            )
            if named:
                # Named targets retain global RDF identity across shape/data
                # ownership. Authored targets express exact role pairs in one
                # native execution rather than a shape/focus cross product.
                targeted = Graph(bind_namespaces="none")
                for triple in qualified.shapes._read(budget=operation):
                    operation.consume("work")
                    operation.consume("triples")
                    operation.consume("states")
                    operation.consume("bytes", 192)
                    targeted.add(triple)
                for binding in named:
                    operation.consume("triples")
                    operation.consume("states")
                    operation.consume("bytes", 192)
                    targeted.add(
                        (shape_iri(binding.role), SH.targetNode, binding.subject)
                    )
                shapes = prepare_rdf(targeted, budget=operation)
                roots = tuple(
                    dict.fromkeys(shape_iri(binding.role) for binding in named)
                )
                pairs = tuple(
                    (str(shape_iri(binding.role)), str(binding.subject))
                    for binding in named
                )
                if evaluate(
                    shapes, (), roots, ShaclCoverageContract(pairs=pairs), "targets"
                ):
                    checked += len(named)
            # Blank focus selectors retain their actual data-owner objects;
            # assigning independent shape-graph blank labels would lose identity.
            for role in ROLES:
                operation.consume("work", len(bindings))
                focus = tuple(
                    binding.subject
                    for binding in bindings
                    if binding.role == role and isinstance(binding.subject, BNode)
                )
                if focus and evaluate(
                    qualified.shapes,
                    focus,
                    (shape_iri(role),),
                    ShaclCoverageContract(minimum_focus=len(focus)),
                    "pairs",
                ):
                    checked += len(focus)
            operation.consume("work", 0)
            complete = checked == len(bindings) and all(c.complete for c in children)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(Issue("catalog.input", str(error), kind="input_error"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return CatalogCheck(
        prepared,
        qualified,
        Report(
            "catalog.selected_profile",
            checked,
            tuple(issues),
            requested=max(1, len(bindings)),
            complete=complete,
            children=tuple(children),
            regime=DCAT_PROFILE,
            exclusions=(
                "unselected-catalog-records",
                "unrequested-extension-semantics",
                "OWL-consistency",
                "temporal-ordering",
            ),
        ),
    )
