"""Bounded native OBO subset contracts over the existing vocabulary inventory."""

from rdflib import URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report
from bijux_ontology.limits import Budget, IssueCollector, bound_text, retain_report
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.results import Edge

from .types import OboSubsetDefinition, OboSubsets

_SUBSET = URIRef("http://www.geneontology.org/formats/oboInOwl#SubsetProperty")
_MEMBER = URIRef("http://www.geneontology.org/formats/oboInOwl#inSubset")


def _edge(edge: Edge, budget: Budget) -> None:
    """Qualify exact term and source metadata before hashing or retaining it."""
    for node in (edge.subject, edge.predicate, edge.object):
        bound_node(node, budget)
    bound_text(edge.scope, budget)


def read_obo_subsets(
    view: VocabularyView, *, closed: bool = False, budget: Budget | None = None
) -> OboSubsets:
    """Read explicitly supplied OBO-in-OWL definitions and membership.

    OBO Format 1.4 maps subsetdef to an annotation subproperty of SubsetProperty,
    and subset to inSubset. This operation checks that finite native convention;
    it neither parses OBO text nor infers logical modules or SKOS declarations.
    Definitions may come from any explicitly selected source of the owned view.
    Closure is an application contract over those sources, never OWL negation.
    """
    if not isinstance(view, VocabularyView) or type(closed) is not bool:
        raise InputError("Expected an owned vocabulary view and explicit closure")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected an optional shared Budget")
    operation = budget if budget is not None else Budget(view.limits)
    issues = IssueCollector(operation)
    definitions: list[OboSubsetDefinition] = []
    memberships: list[Edge] = []
    assertions: list[Edge] = []
    links: dict[URIRef, list[Edge]] = {}
    metadata: dict[URIRef, list[Edge]] = {}
    children: list[Report] = []
    complete = True
    checked = 0

    def issue(code: str, message: str, edge: Edge, *, unknown: bool = False) -> None:
        """Locate only actual assertions with already qualified source metadata."""
        issues.append(
            Issue(
                code,
                message,
                str(edge.subject),
                kind="unverified" if unknown else "finding",
                details=Location(source=edge.scope, term=str(edge.subject)),
            )
        )

    try:
        selected = view.all_statements(budget=operation)
        retain_report(selected.report, operation, issues)
        children.append(selected.report)
        complete = selected.report.complete
        for edge in selected.items:
            operation.consume("work")
            _edge(edge, operation)
            if edge.predicate != _MEMBER and not (
                edge.predicate == RDFS.subPropertyOf and edge.object == _SUBSET
            ):
                continue
            operation.consume("states")
            assertions.append(edge)
            if edge.predicate == _MEMBER:
                operation.consume("states")
                memberships.append(edge)
            elif isinstance(edge.subject, URIRef):
                if edge.subject not in links:
                    operation.consume("states", 2)
                    links[edge.subject] = []
                    metadata[edge.subject] = []
                operation.consume("states")
                links[edge.subject].append(edge)
            else:
                complete = False
                issue(
                    "obo.subset_definition_kind",
                    "OBO subset definitions require a named annotation property",
                    edge,
                    unknown=not closed,
                )
            checked += 1
        for edge in selected.items:
            operation.consume("work")
            if isinstance(edge.subject, URIRef) and edge.subject in links:
                _edge(edge, operation)
                operation.consume("states")
                metadata[edge.subject].append(edge)
        for iri, witnesses in links.items():
            operation.consume("work")
            operation.consume("states")
            outgoing = tuple(metadata[iri])
            definitions.append(OboSubsetDefinition(iri, tuple(witnesses), outgoing))
            kinds = set()
            for edge in outgoing:
                operation.consume("work")
                if edge.predicate == RDF.type:
                    operation.consume("states")
                    kinds.add(edge.object)
            if kinds.intersection((OWL.ObjectProperty, OWL.DatatypeProperty)):
                issue(
                    "obo.subset_definition_kind",
                    "Subset definition has a conflicting logical property kind",
                    witnesses[0],
                )
            elif OWL.AnnotationProperty not in kinds:
                complete = False
                issue(
                    "obo.subset_unverified",
                    "Subset annotation-property declaration is not supplied",
                    witnesses[0],
                    unknown=True,
                )
        for edge in memberships:
            operation.consume("work")
            if not isinstance(edge.object, URIRef):
                complete = False
                issue(
                    "obo.subset_membership_kind",
                    "inSubset requires a named subset IRI; original assertion retained",
                    edge,
                    unknown=not closed,
                )
            elif edge.object not in links:
                issue(
                    "obo.subset_undefined",
                    "inSubset references an undefined subset in the selected scope",
                    edge,
                    unknown=not closed or not selected.report.complete,
                )
        if not closed:
            complete = False
            issues.append(
                Issue(
                    "obo.subset_unverified",
                    "Open subset inventory cannot establish complete "
                    "allowed-set coverage",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return OboSubsets(
        tuple(definitions),
        tuple(memberships),
        tuple(assertions),
        closed,
        Report(
            "obo.subsets",
            checked,
            tuple(issues),
            allow_empty=True,
            requested=checked if complete else checked + 1,
            complete=complete,
            regime="asserted-obo-subsets",
            children=tuple(children),
        ),
        view.limits,
    )
