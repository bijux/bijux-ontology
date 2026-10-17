"""Prepare explicit vocabulary sources and their required import closure."""

from collections.abc import Mapping, Sequence

from rdflib import Graph

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.jsonld.resources import DocumentLoader, context_resources
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource

from .imports import required_imports
from .view import Vocabulary


def load_vocabulary(
    source: RdfSource | Payload,
    *,
    base: str | None = None,
    format: str | None = None,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    imports: Mapping[str, RdfSource | Payload] | None = None,
    lookup: Sequence[Graph | PreparedSource | RdfSource] = (),
    limits: Limits | None = None,
    preserve_literals: bool = True,
    document_loader: DocumentLoader | None = None,
) -> Vocabulary:
    """Load explicit scoped resources without implicit file/network resolution.

    Text/bytes/streams require a syntax; datasets require graph selection or
    explicit union. Snapshot files keep their logical base. Lookup documents do
    not form a required import closure. Limits apply before owned source copies.
    """
    resources = context_resources(document_loader, limits=limits)
    budget = resources.budget
    selected = budget.limits
    prepared = prepare_source(
        source,
        base=base,
        format=format,
        limits=selected,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=resources,
    )
    local = prepared._read(graph_name, union=union, budget=budget)
    imported = required_imports(
        prepared,
        local,
        imports,
        budget=budget,
        preserve_literals=preserve_literals,
        document_loader=resources,
    )
    lookups = [
        prepare_source(
            item._read() if isinstance(item, PreparedSource) else item,
            limits=selected,
            budget=budget,
            preserve_literals=preserve_literals,
            document_loader=resources,
        )._read(budget=budget)
        for item in lookup
    ]
    return Vocabulary._from_owned(local, imported, tuple(lookups), selected)
