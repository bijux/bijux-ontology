"""Prepare explicit vocabulary sources and their required import closure."""

from collections.abc import Mapping, Sequence

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.processing.resources import DocumentLoader, context_resources
from bijux_ontology.rdf.sources.loading import _charge_graph
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.vocabulary.sources.imports import (
    _required_imports_owned,
    required_imports,
)
from bijux_ontology.vocabulary.sources.types import OwnedSource
from bijux_ontology.vocabulary.sources.view import Vocabulary


def load_vocabulary(
    source: PreparedSource | RdfSource | Payload,
    *,
    base: str | None = None,
    format: str | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    lookup: Sequence[Graph | PreparedSource | RdfSource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    document_loader: DocumentLoader | None = None,
) -> Vocabulary:
    """Load explicit scoped resources without implicit file/network resolution.

    Text/bytes/streams require a syntax; datasets require graph selection or
    explicit union. Snapshot files keep their logical base. Lookup documents do
    not form a required import closure. Limits apply before owned source copies."""
    return _load_vocabulary(
        source,
        base=base,
        format=format,
        graph_name=graph_name,
        union=union,
        imports=imports,
        lookup=lookup,
        limits=limits,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=document_loader,
        owned_worker=False,
    )


def _load_vocabulary_owned(
    source: PreparedSource | RdfSource | Payload,
    *,
    base: str | None = None,
    format: str | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    lookup: Sequence[Graph | PreparedSource | RdfSource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    document_loader: DocumentLoader | None = None,
) -> Vocabulary:
    """Prepare actual required sources inside the supervising disposable worker."""
    return _load_vocabulary(
        source,
        base=base,
        format=format,
        graph_name=graph_name,
        union=union,
        imports=imports,
        lookup=lookup,
        limits=limits,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=document_loader,
        owned_worker=True,
    )


def _load_vocabulary(
    source: PreparedSource | RdfSource | Payload,
    *,
    base: str | None = None,
    format: str | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    lookup: Sequence[Graph | PreparedSource | RdfSource] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    document_loader: DocumentLoader | None = None,
    owned_worker: bool,
) -> Vocabulary:
    """Share original input and result qualification for fixed owned routes."""
    if type(owned_worker) is not bool:
        raise InputError("Owned vocabulary loading selection must be a boolean")
    resources = context_resources(document_loader, limits=limits, budget=budget)
    budget = resources.budget
    selected = budget.limits
    prepared = (
        source
        if isinstance(source, PreparedSource)
        else prepare_source(
            source,
            base=base,
            format=format,
            limits=selected,
            budget=budget,
            preserve_literals=preserve_literals,
            document_loader=resources,
        )
    )
    if isinstance(source, PreparedSource) and (base is not None or format is not None):
        raise InputError("Prepared vocabulary sources retain their format and base")
    local = OwnedSource(
        prepared,
        prepared._read(graph_name, union=union, budget=budget),
        graph_name,
        union,
    )
    if isinstance(source, PreparedSource):
        budget.consume("resources")
        _charge_graph(local.selected, budget, selected.max_bytes)
    import_closure = _required_imports_owned if owned_worker else required_imports
    imported = import_closure(
        prepared,
        local,
        imports,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=resources,
    )
    lookups = []
    for item in lookup:
        resource = (
            item
            if isinstance(item, PreparedSource)
            else prepare_source(
                item,
                limits=selected,
                budget=budget,
                preserve_literals=preserve_literals,
                document_loader=resources,
            )
        )
        graph = resource._read(budget=budget)
        if isinstance(item, PreparedSource):
            budget.consume("resources")
            _charge_graph(graph, budget, selected.max_bytes)
        lookups.append(OwnedSource(resource, graph))
    return Vocabulary._from_owned(
        local,
        imported,
        tuple(lookups),
        selected,
        required_imports_qualified=True,
        admitted_budget=budget,
    )
