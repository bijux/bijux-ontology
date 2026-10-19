"""Offline acquisition of one exact supplied Schema.org release and selection."""

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import replace

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_input, prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_node

from .types import SchemaOrgView, _iri, _metadata


def _view(view: SchemaOrgView, budget: Budget) -> Graph | None:
    """Requalify public metadata and the owned pinned read in the current budget."""
    if not isinstance(view, SchemaOrgView):
        raise InputError("Expected an explicit SchemaOrgView")
    bound_text(view.release, budget)
    bound_text(view.sha256, budget)
    if view.syntax is not None:
        bound_text(view.syntax, budget)
    if view.namespace is None and view.source is None:
        return None
    _metadata(
        view.release,
        _iri(view.namespace, budget),
        view.extensions,
        view.iri_mapping,
        budget,
    )
    if view.source is None:
        return None
    if len(view.sha256) != 64 or any(
        char not in "0123456789abcdef" for char in view.sha256
    ):
        raise InputError("Schema.org byte provenance requires a SHA256 digest")
    if not isinstance(view.source, PreparedSource):
        raise InputError("Schema.org views require an owned PreparedSource")
    for field in (view.source.document_iri, view.source.effective_url):
        if field is not None:
            bound_text(field, budget)
            absolute_iri(field)
    return view.source._read(budget=budget)


def _mapped(view: SchemaOrgView, iri: URIRef, budget: Budget) -> URIRef:
    """Apply only an explicit finite alias choice; never rewrite the actual RDF."""
    _iri(iri, budget)
    for key, target in view.iri_mapping:
        budget.consume("work", len(key) + len(iri) + 1)
        if key == iri:
            return target
    return iri


def _included(view: SchemaOrgView, graph: Graph, iri: URIRef, budget: Budget) -> bool:
    """Read exact release section membership without namespace guesses."""
    _iri(iri, budget)
    memberships = False
    selected = False
    predicate = URIRef(str(view.namespace) + "isPartOf")
    for value in graph.objects(iri, predicate):
        budget.consume("work")
        bound_node(value, budget)
        memberships = True
        if not isinstance(value, URIRef):
            return False
        for wanted in view.extensions:
            budget.consume("work", len(wanted) + len(value) + 1)
            selected |= value == wanted
    return not memberships or selected


def prepare_schemaorg(
    source: RdfSource,
    *,
    release: str,
    namespace: URIRef,
    extensions: Sequence[URIRef] = (),
    iri_mapping: Sequence[tuple[URIRef, URIRef]] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> SchemaOrgView:
    """Prepare an offline, byte-pinned release using the ordinary RDF loader.

    RdfSource supplies actual bytes/path/stream, syntax, logical identity and exact
    graph or union choice. URLs are never fetched. The external input is acquired
    once; the maintained parser consumes those owned bytes. The digest describes
    decompressed RDF bytes, not a package signature or remote release authenticity.
    """
    if not isinstance(source, RdfSource) or isinstance(source.content, Graph):
        raise InputError("A pinned Schema.org release requires an RDF byte descriptor")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    parsed = None
    qualified_namespace = None
    selected: tuple[URIRef, ...] = ()
    mapping: tuple[tuple[URIRef, URIRef], ...] = ()
    label = ""
    digest = ""
    syntax = None
    checked = 0
    try:
        selected, mapping = _metadata(
            release, namespace, extensions, iri_mapping, operation
        )
        label = str(release)
        qualified_namespace = namespace
        if source.document_iri is None:
            raise InputError("Pinned releases require a supplied logical document IRI")
        for field in (
            source.document_iri,
            source.effective_url,
            source.format,
            source.graph_name,
            source.location,
            source.role,
        ):
            if field is not None:
                bound_text(field, operation)
        prepared = prepare_input(source, budget=operation)
        payload = prepared.read()
        syntax = prepared.syntax
        operation.consume("work", len(payload) + 1)
        operation.consume("bytes", 64)
        digest = hashlib.sha256(payload).hexdigest()
        parsed = prepare_rdf(
            replace(source, content=payload, format=prepared.syntax, compression=None),
            budget=operation,
        )
        graph = parsed._read(budget=operation)
        for _, target in mapping:
            operation.consume("work")
            if not any(graph.triples((target, RDF.type, None))):
                raise InputError(
                    "IRI mapping target lacks a supplied release declaration"
                )
        for statement in graph.triples((None, OWL.imports, None)):
            operation.consume("work")
            for term in statement:
                bound_node(term, operation)
            issues.append(
                Issue(
                    "schemaorg.expectation_unverified",
                    "Selected release imports resources outside the supplied snapshot",
                    kind="unverified",
                )
            )
        checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return SchemaOrgView._from_owned(
        parsed,
        label,
        qualified_namespace,
        digest,
        syntax,
        selected,
        mapping,
        Report(
            "schemaorg.preparation",
            checked,
            tuple(issues),
            requested=1,
            regime="supplied-schemaorg-release",
        ),
    )
