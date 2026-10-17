"""Requested import aliases resolve once, without first-winner ambiguity."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from rdflib import Graph, URIRef
from rdflib.namespace import OWL

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource

from ..errors import InputError, ResourceError


@dataclass(frozen=True)
class _Binding:
    selected: Graph


def _same(first: _Binding, second: _Binding, budget: Budget, alias: str) -> None:
    # Required alias agreement reuses the maintained bounded RDF comparison.
    from bijux_ontology.rdf.comparison import compare_rdf

    report = compare_rdf(first.selected, second.selected, budget=budget)
    if not report.verified:
        raise ResourceError(
            "Conflicting or unverified supplied sources for a required document alias",
            location=alias,
        )


def required_imports(
    prepared: PreparedSource,
    local: Graph,
    supplied: Mapping[str, RdfSource | Payload] | None,
    *,
    budget: Budget,
    preserve_literals: bool,
    document_loader: DocumentLoader | None = None,
) -> tuple[Graph, ...]:
    """Resolve requested keys only; lookup/header IRIs never establish aliases."""
    root = _Binding(local)
    aliases = {
        iri: root
        for iri in (prepared.document_iri, prepared.effective_url)
        if iri is not None
    }
    requested: set[str] = set()
    loaded: list[Graph] = []
    pending = [local]
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
            candidate = _Binding(resource.graph())
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
                loaded.append(imported)
                pending.append(imported)
            for name in names:
                aliases[name] = representative
    return tuple(loaded)
