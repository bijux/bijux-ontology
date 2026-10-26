"""Model output contracts over explicitly selected owned RDF subjects."""

from collections.abc import Sequence
from typing import cast

from rdflib import Graph

from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.jsonld.processing.conversion import DirectionPolicy
from bijux_ontology.models.emission.types import RdfModelUsage
from bijux_ontology.models.rdf.field_obligations import RdfFieldObligations
from bijux_ontology.models.rdf.preparation import (
    RdfBindingDiscovery,
    prepare_model_obligations,
)
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.sources.selection import select_read
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.view import Vocabulary


def check_rdf_emission(
    source: PreparedSource,
    models: Sequence[ModelDeclaration],
    usages: Sequence[RdfModelUsage],
    *,
    vocabulary: Vocabulary | Graph | VocabularyView | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Check actual selected RDF using the existing value and occurrence contracts.

    Graph selection applies to every subject, endpoint, type and RDF-list link.
    Unrelated subjects are checked only through unambiguous declared child links.
    No OWL entailment or source JSON null/omission is inferred. require_property
    remains source-presence unknown; guaranteed emission and minimum_values
    express required RDF predicate occurrences. Exact native literal validation
    and counts use the prepared source's available literal fidelity.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    if not isinstance(source, PreparedSource):
        raise InputError("RDF emission checking requires an owned PreparedSource")
    if isinstance(cast(object, usages), (str, bytes)) or not isinstance(
        usages, Sequence
    ):
        raise InputError("Expected RDF subject bindings")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    budget = budget if budget is not None else Budget(limits)
    issues = IssueCollector(budget)
    enumeration_reports: dict[tuple[int, bool], Report] = {}
    obligations: RdfFieldObligations | None = None
    complete = True
    role_checks: _RoleChecks | None = None
    try:
        registry, needs_roles = prepare_model_obligations(
            models, budget, issues, enumeration_reports
        )
        graph = select_read(source, budget, graph_name=graph_name, union=union).read()
        role_checks = (
            _RoleChecks(vocabulary, budget, graph)
            if vocabulary is not None and needs_roles
            else None
        )

        bindings = RdfBindingDiscovery(graph, registry, budget, issues)
        bindings.admit(usages)
        obligations = RdfFieldObligations(
            source,
            graph,
            registry,
            bindings,
            budget,
            issues,
            role_checks,
            rdf_direction,
            datatype_registry,
        )
        for binding in bindings.pending:
            obligations.check_binding(binding)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf-emission")
        complete = False
    checked = obligations.checked if obligations is not None else 0
    return Report(
        "rdf-emission",
        checked,
        tuple(issues),
        children=(
            *enumeration_reports.values(),
            *(
                (role_checks.exhaustion,)
                if role_checks is not None and role_checks.exhaustion is not None
                else ()
            ),
        ),
        requested=checked if complete else checked + 1,
        complete=complete,
        regime="actual-rdf-output",
        families=("model-output",),
    )
