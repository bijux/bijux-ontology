"""Controlled JSON-LD conversion into owned RDFLib graphs and datasets."""

from dataclasses import dataclass, replace
from typing import cast

from rdflib import BNode, Dataset, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.conversion import DirectionPolicy, _convert_to_rdf_dataset
from bijux_ontology.jsonld.resources import (
    DocumentLoader,
    context_resources,
)
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_term
from bijux_ontology.reports import Issue, Report


@dataclass(frozen=True)
class JSONLDPreparation:
    """An owned source or an explicit conversion/interoperability limitation."""

    source: PreparedSource | None
    report: Report


def _term(value: object, nodes: dict[str, BNode], budget: Budget) -> Node:
    budget.consume("work")
    if not isinstance(value, dict):
        raise InputError("The RDF processor returned an unsupported term shape")
    term = cast(dict[str, object], value)
    text, kind = term.get("value"), term.get("type")
    if not isinstance(text, str):
        raise InputError("The RDF processor returned a non-text RDF term")
    if kind == "IRI":
        return URIRef(absolute_iri(text))
    if kind == "blank node":
        if text not in nodes:
            budget.consume("states")
            nodes[text] = BNode()
        return nodes[text]
    if kind == "literal":
        datatype, language = term.get("datatype"), term.get("language")
        if datatype is not None and not isinstance(datatype, str):
            raise InputError("The RDF processor returned a non-IRI datatype")
        if language is not None and not isinstance(language, str):
            raise InputError("The RDF processor returned a non-text language")
        return literal_term(text, datatype=datatype, language=language)
    raise InputError("The RDF processor returned an unsupported RDF term kind")


def _dataset(data: dict[str, object], budget: Budget) -> Dataset:
    result = Dataset()
    nodes: dict[str, BNode] = {}
    for name, triples in data.items():
        budget.consume("work")
        if name == "@default":
            graph = result.default_graph
        else:
            budget.consume("states")
            name_node = _term(
                {
                    "type": "blank node" if name.startswith("_:") else "IRI",
                    "value": name,
                },
                nodes,
                budget,
            )
            if not isinstance(name_node, (URIRef, BNode)):
                raise InputError("RDF graph names must be resources")
            graph = result.graph(name_node)
        if not isinstance(triples, list):
            raise InputError("The RDF processor returned an unsupported graph shape")
        for triple in triples:
            budget.consume("work")
            if not isinstance(triple, dict):
                raise InputError("The RDF processor returned an unsupported triple")
            subject, predicate, value = (
                _term(triple.get(role), nodes, budget)
                for role in ("subject", "predicate", "object")
            )
            if not isinstance(subject, (URIRef, BNode)) or not isinstance(
                predicate, URIRef
            ):
                raise InputError(
                    "RDF subjects must be resources and predicates must be IRIs"
                )
            graph.add((subject, predicate, value))
    return result


def prepare_jsonld(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    document_iri: str | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> JSONLDPreparation:
    """Convert once with explicit resources, retaining graph and literal identity.

    Unlike N-Quads alone, the owned dataset keeps empty named graphs. Relative
    references require an actual base, and direction-sensitive literals require
    an explicit RDF policy. Valid unsupported RDFLib literal preservation returns
    an unverified report without a source; programmer errors in caller loaders
    propagate. Source exports are independent copies.
    """
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    if document_iri is not None:
        absolute_iri(document_iri)
    try:
        converted = _convert_to_rdf_dataset(
            document,
            base=base,
            document_loader=resources,
            allowed_drops=allowed_drops,
            direction=direction,
            budget=operation,
        )
    except LimitExceeded as error:
        return JSONLDPreparation(
            None,
            Report(
                "jsonld.source",
                issues=(
                    Issue("execution.limit_exceeded", str(error), kind="exhausted"),
                ),
                requested=1,
                complete=False,
                regime="jsonld_to_rdf",
            ),
        )
    except InputError as error:
        return JSONLDPreparation(
            None,
            Report(
                "jsonld.source",
                issues=(Issue("context.input_error", str(error), kind="input_error"),),
                requested=1,
                complete=False,
                regime="jsonld_to_rdf",
            ),
        )
    if converted.dataset is None:
        return JSONLDPreparation(None, replace(converted.report, scope="jsonld.source"))
    issues = IssueCollector(operation)
    try:
        dataset = _dataset(converted.dataset, operation)
        effective = (
            resources.effective_url(document)
            if isinstance(document, str)
            else document_iri
        )
        source = PreparedSource(
            dataset,
            is_dataset=any(name != "@default" for name in converted.dataset),
            document_iri=document_iri
            or (document if isinstance(document, str) else None),
            effective_url=effective,
            literal_fidelity="source_terms",
            graph_fidelity="complete",
            max_triples=operation.limits.max_triples,
        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "context.rdf_interoperability_unverified",
                    str(error),
                    kind="unverified",
                )
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    else:
        return JSONLDPreparation(
            source, replace(converted.report, scope="jsonld.source")
        )
    return JSONLDPreparation(
        None,
        replace(
            converted.report,
            scope="jsonld.source",
            issues=converted.report.issues + tuple(issues),
            complete=False,
        ),
    )
