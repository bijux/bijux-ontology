"""Original alternative hints, section selection and explicit inverse witnesses."""

from itertools import chain

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.terms import bound_node

from .preparation import _included, _mapped, _view
from .types import SchemaOrgView, SchemaPropertyExpectation, Triple, _iri


def _edges(
    graph: Graph, node: URIRef, predicate: URIRef, budget: Budget
) -> tuple[Triple, ...]:
    """Retain only paid exact original assertions from the existing graph index."""
    result = []
    for triple in graph.triples((node, predicate, None)):
        budget.consume("work")
        for term in triple:
            bound_node(term, budget)
        budget.consume("states")
        budget.consume("bytes", 64)
        result.append(triple)
    return tuple(result)


def schemaorg_property(
    view: SchemaOrgView,
    predicate: URIRef,
    *,
    reverse: bool = False,
    budget: Budget | None = None,
) -> SchemaPropertyExpectation:
    """Read direct endpoint alternatives, with exact one-hop inverse fallback.

    Multiple includes values are alternatives, never conjunctive RDFS axioms.
    Direct hints take precedence over inverse fallback for each endpoint. A
    missing endpoint described only by a superproperty remains unknown in this
    initial application dialect; no inherited hint is silently conjoined. Named
    class inheritance is handled separately when matching actual instance types.
    """
    if type(reverse) is not bool:
        raise InputError("Schema.org property orientation requires a boolean")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Schema.org property reads require an optional shared Budget")
    operation = budget if budget is not None else Budget()
    issues = IssueCollector(operation)
    actual = None
    requested = None
    domains: list[Node] = []
    ranges: list[Node] = []
    inverses: list[Node] = []
    replacements: list[Node] = []
    assertions: list[Triple] = []
    children: tuple[Report, ...] = ()
    checked = 0

    def unknown(message: str) -> None:
        """Never render an unqualified term into a failure location."""
        issues.append(
            Issue("schemaorg.expectation_unverified", message, kind="unverified")
        )

    try:
        _iri(predicate, operation)
        requested = predicate
        graph = _view(view, operation)
        retain_report(view.report, operation, issues)
        children = (view.report,)
        actual = _mapped(view, predicate, operation)
        if graph is None:
            unknown("Required supplied Schema.org release is unavailable")
        elif not _included(view, graph, actual, operation):
            unknown("Selected property is outside the chosen release sections")
        else:
            kinds = _edges(graph, actual, RDF.type, operation)
            assertions.extend(kinds)
            if not any(
                edge[2] in (RDF.Property, URIRef(str(view.namespace) + "Property"))
                for edge in kinds
            ):
                unknown(
                    "Selected property lacks a supplied Schema.org property declaration"
                )
            domain_predicate = URIRef(str(view.namespace) + "domainIncludes")
            range_predicate = URIRef(str(view.namespace) + "rangeIncludes")
            inverse_predicate = URIRef(str(view.namespace) + "inverseOf")
            for relation, values in (
                (domain_predicate, domains),
                (range_predicate, ranges),
                (URIRef(str(view.namespace) + "supersededBy"), replacements),
            ):
                edges = _edges(graph, actual, relation, operation)
                assertions.extend(edges)
                for edge in edges:
                    operation.consume("states")
                    values.append(edge[2])
                    if not isinstance(edge[2], URIRef):
                        unknown("Anonymous or literal expectations are unsupported")
            for expected in chain(domains, ranges):
                operation.consume("work")
                if not isinstance(expected, URIRef):
                    continue
                target = _mapped(view, expected, operation)
                if not _included(view, graph, target, operation) or not any(
                    edge[2]
                    in (
                        RDFS.Class,
                        OWL.Class,
                        URIRef(str(view.namespace) + "Class"),
                        URIRef(str(view.namespace) + "DataType"),
                    )
                    for edge in _edges(graph, target, RDF.type, operation)
                ):
                    unknown(
                        "An includes target lacks a selected named-class definition"
                    )
            direct_domain, direct_range = bool(domains), bool(ranges)
            inverse_edges = list(_edges(graph, actual, inverse_predicate, operation))
            for triple in graph.triples((None, inverse_predicate, actual)):
                operation.consume("work")
                for term in triple:
                    bound_node(term, operation)
                operation.consume("states")
                inverse_edges.append(triple)
            for edge in inverse_edges:
                operation.consume("work")
                assertions.append(edge)
                other = edge[2] if edge[0] == actual else edge[0]
                operation.consume("states")
                inverses.append(other)
                if not isinstance(other, URIRef) or not _included(
                    view, graph, other, operation
                ):
                    unknown("An inverse is outside the selected supported sections")
                    continue
                for relation, values, direct in (
                    (range_predicate, domains, direct_domain),
                    (domain_predicate, ranges, direct_range),
                ):
                    if direct:
                        continue
                    for witness in _edges(graph, other, relation, operation):
                        operation.consume("states", 2)
                        assertions.append(witness)
                        values.append(witness[2])
                        if not isinstance(witness[2], URIRef):
                            unknown("An inverse hint has an unsupported endpoint")
            parents = _edges(graph, actual, RDFS.subPropertyOf, operation)
            assertions.extend(parents)
            if parents and (not domains or not ranges):
                unknown("Inherited-only hints are not direct application expectations")
            if not domains and not ranges:
                unknown("Selected property supplies no supported endpoint expectations")
            checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    if reverse:
        domains, ranges = ranges, domains
    return SchemaPropertyExpectation(
        requested,
        actual,
        tuple(domains),
        tuple(ranges),
        tuple(inverses),
        tuple(replacements),
        tuple(assertions),
        Report(
            "schemaorg.property",
            checked,
            tuple(issues),
            requested=1,
            children=children,
            regime="schemaorg-published-expectations",
        ),
    )
