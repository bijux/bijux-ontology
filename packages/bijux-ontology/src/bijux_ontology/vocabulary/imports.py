"""Requested import aliases resolve once, without first-winner ambiguity."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource

from ..errors import InputError, ResourceError
from .sources import OwnedSource


@dataclass(frozen=True)
class _Binding:
    source: OwnedSource

    @property
    def selected(self) -> Graph:
        """Reuse the already selected graph for import closure and alias checks."""
        return self.source.selected


def _selected_contexts(source: OwnedSource, budget: Budget) -> tuple[set[str], Dataset]:
    """Project only an alias's selected contexts for maintained RDF comparison."""
    if source.prepared.graph_fidelity != "complete":
        raise ResourceError("Required alias graph ownership is unverified")
    dataset = Dataset()
    kinds: set[str] = set()
    for kind, name, graph in source.contexts(budget):
        budget.consume("states")
        kinds.add(kind)
        target = dataset.default_graph if name is None else dataset.graph(name)
        for triple in graph:
            budget.consume("work")
            budget.consume("triples")
            budget.consume("states")
            target.add(triple)
    return kinds, dataset


def _same(first: _Binding, second: _Binding, budget: Budget, alias: str) -> None:
    # Required alias agreement reuses the maintained bounded RDF comparison.
    from bijux_ontology.rdf.comparison import compare_rdf

    left, right = first.selected, second.selected
    if first.source.prepared.is_dataset or second.source.prepared.is_dataset:
        left_kinds, left = _selected_contexts(first.source, budget)
        right_kinds, right = _selected_contexts(second.source, budget)
        if left_kinds != right_kinds:
            raise ResourceError(
                "Conflicting selected graph ownership for a required document alias",
                location=alias,
            )
    report = compare_rdf(left, right, budget=budget)
    if not report.verified:
        raise ResourceError(
            "Conflicting or unverified supplied sources for a required document alias",
            location=alias,
        )


def required_imports(
    prepared: PreparedSource,
    local: OwnedSource,
    supplied: Mapping[str, RdfSource | Payload] | None,
    *,
    budget: Budget,
    preserve_literals: bool,
    document_loader: DocumentLoader | None = None,
) -> tuple[OwnedSource, ...]:
    """Resolve requested keys only; lookup/header IRIs never establish aliases."""
    root = _Binding(local)
    aliases = {
        iri: root
        for iri in (prepared.document_iri, prepared.effective_url)
        if iri is not None
    }
    requested: set[str] = set()
    loaded: list[OwnedSource] = []
    pending = [local.selected]
    while pending:
        graph = pending.pop()
        for reference in sorted(graph.objects(None, OWL.imports), key=str):
            budget.consume("work")
            if not isinstance(reference, URIRef):
                raise InputError("Ontology imports must be absolute IRIs")
            iri = absolute_iri(str(reference))
            if iri in requested:
                continue
            requested.add(iri)
            existing = aliases.get(iri)
            if supplied is None or iri not in supplied:
                if existing is not None:
                    continue
                raise ResourceError(
                    "Required import has no supplied source", location=iri
                )
            raw = supplied[iri]
            document = (
                replace(raw, document_iri=raw.document_iri or iri)
                if isinstance(raw, RdfSource)
                else RdfSource(raw, document_iri=iri)
            )
            resource = prepare_source(
                document,
                limits=budget.limits,
                budget=budget,
                preserve_literals=preserve_literals,
                document_loader=document_loader,
            )
            candidate = _Binding(OwnedSource(resource, resource._read(budget=budget)))
            names = {
                value
                for value in (iri, resource.document_iri, resource.effective_url)
                if value is not None
            }
            collisions = {
                id(aliases[name]): aliases[name] for name in names if name in aliases
            }
            if len(collisions) > 1:
                raise ResourceError(
                    "Required source joins previously distinct document aliases",
                    location=iri,
                )
            if collisions:
                representative = next(iter(collisions.values()))
                _same(representative, candidate, budget, iri)
            else:
                representative = candidate
                imported = candidate.selected
                loaded.append(candidate.source)
                pending.append(imported)
            for name in names:
                aliases[name] = representative
    return tuple(loaded)
