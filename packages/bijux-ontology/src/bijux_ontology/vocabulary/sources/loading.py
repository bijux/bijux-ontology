"""Prepare explicit vocabulary sources and their required import closure."""

from collections.abc import Mapping, Sequence

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.jsonld.resources import DocumentLoader, context_resources
from bijux_ontology.rdf.loading import _charge_graph
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.vocabulary.sources.imports import required_imports
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
    _owned_worker: bool = False,
) -> Vocabulary:
    """Load explicit scoped resources without implicit file/network resolution.

    Text/bytes/streams require a syntax; datasets require graph selection or
    explicit union. Snapshot files keep their logical base. Lookup documents do
    not form a required import closure. Limits apply before owned source copies.
    """
    if type(_owned_worker) is not bool:
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
    imported = required_imports(
        prepared,
        local,
        imports,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=resources,
        _owned_worker=_owned_worker,
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
        local, imported, tuple(lookups), selected, required_imports_qualified=True
    )
