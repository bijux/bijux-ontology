"""Vocabulary references and actual literal values in explicitly selected RDF."""

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary


def check_data_usage(
    data: Graph | PreparedSource,
    vocabulary: Vocabulary | Graph,
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    exclude: tuple[str, ...] = (),
    check_types: bool = True,
    check_literals: bool = True,
    limits: Limits | None = None,
    datatype_registry: DatatypeRegistry | None = None,
) -> Report:
    """Check vocabulary positions, not ordinary instance identifiers.

    ``rdf:type`` objects are class references; other resource objects are data.
    Dataset selection is explicit. Lexical checking examines supplied literals
    only and retains unsupported datatype outcomes without rewriting values.
    """
    if datatype_registry is not None and not isinstance(
        datatype_registry, DatatypeRegistry
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    selected = limits if limits is not None else Limits()
    budget = Budget(selected)
    prepared = (
        data if isinstance(data, PreparedSource) else prepare_rdf(data, limits=selected)
    )
    graph = prepared.graph(graph_name, union=union)
    sources = (
        vocabulary
        if isinstance(vocabulary, Vocabulary)
        else load_vocabulary(vocabulary, limits=selected)
    )
    excluded = frozenset(absolute_iri(iri) for iri in exclude)
    if type(check_types) is not bool or type(check_literals) is not bool:
        raise InputError("check_types and check_literals must be booleans")
    issues = IssueCollector(budget)
    checked = 0
    complete = True
    try:
        budget.consume("triples", len(graph))
        for subject, predicate, value in sorted(
            graph, key=lambda triple: tuple(map(str, triple))
        ):
            budget.consume("work")
            location = f"{subject} {predicate}"
            terms = []
            if str(predicate) not in excluded:
                terms.append(
                    TermDeclaration(str(predicate), (str(RDF.Property),), location)
                )
            if (
                check_types
                and predicate == RDF.type
                and isinstance(value, URIRef)
                and str(value) not in excluded
            ):
                terms.append(
                    TermDeclaration(
                        str(value), (str(OWL.Class), str(RDFS.Class)), location
                    )
                )
            if check_types and predicate == RDF.type and not isinstance(value, URIRef):
                issues.append(
                    Issue(
                        "data.type_unverified",
                        "Non-IRI class expressions need unsupported type comparison",
                        location,
                        kind="unverified",
                        actual=str(value),
                    )
                )
            if terms:
                report = check_vocabulary(sources, terms)
                checked += report.checked
                issues.extend(report.issues)
            if check_literals and isinstance(value, Literal):
                report = check_literal(
                    value,
                    location=location,
                    datatype_registry=datatype_registry,
                    budget=budget,
                )
                checked += report.checked
                issues.extend(report.issues)
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    return Report(
        "data-usage",
        checked,
        tuple(issues),
        complete=complete,
        exclusions=tuple(sorted(excluded)),
        families=(
            "vocabulary-usage",
            *(("literal-samples",) if check_literals else ()),
        ),
    )
