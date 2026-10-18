"""Exact original RDF document staging shared by explicit native OWL operations."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from rdflib import ConjunctiveGraph, Dataset, Graph

from bijux_ontology.documents.export import export_rdf
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.terms import (
    bound_node,
    bound_text,
    independent_blank_scopes,
)
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.sources import source_role

from .types import ProfileDocument


class _ImportOrigins(Mapping[str, RdfSource | Payload]):
    """Observe core's actual reads without discovering or resolving imports."""

    def __init__(self, sources: dict[str, RdfSource | Payload], budget: Budget) -> None:
        """Keep only this operation's finite captured registry and read evidence."""
        self.sources = sources
        self.budget = budget
        self.originals: dict[str, RdfSource | Payload] = {}

    def __getitem__(self, key: str) -> RdfSource | Payload:
        """Record the first actual supplied source for a core-selected document."""
        raw = self.sources[key]
        document = raw.document_iri or key if isinstance(raw, RdfSource) else key
        bound_text(document, self.budget)
        identity = str(document)
        if identity not in self.originals:
            self.budget.consume("states")
            self.originals[identity] = raw
        return raw

    def __contains__(self, key: object) -> bool:
        """Membership alone does not establish a selected source role."""
        return key in self.sources

    def __iter__(self) -> Iterator[str]:
        """Expose the captured registry without treating iteration as a read."""
        return iter(self.sources)

    def __len__(self) -> int:
        """Retain the already finite registry size."""
        return len(self.sources)


def _original_graph(raw: RdfSource | Payload, budget: Budget) -> Graph | None:
    """Read caller term identities within exactly the successful selected scope."""
    resource = raw if isinstance(raw, RdfSource) else RdfSource(raw)
    content = resource.content
    if isinstance(content, Dataset):
        if resource.union:
            raise ResourceError("Imported OWL documents require one physical graph")
        # A transient read handle preserves original term objects for this guard;
        # normal core preparation still owns and validates the actual import.
        budget.consume("states")
        view = PreparedSource._from_owned(
            content,
            is_dataset=True,
            document_iri=resource.document_iri,
            effective_url=resource.effective_url,
            literal_fidelity="supplied_terms",
            graph_name=resource.graph_name,
        )
        return view._read(budget=budget)
    if isinstance(content, ConjunctiveGraph):
        raise ResourceError("Original legacy conjunctive graph scope is unsupported")
    return content if isinstance(content, Graph) else None


def stage_documents(
    source: PreparedSource,
    supplied: Mapping[str, RdfSource | Payload] | None,
    directory: Path,
    budget: Operation,
    issues: IssueCollector,
    reports: list[Report],
) -> tuple[tuple[ProfileDocument, ...], dict[str, Any], tuple[Report, ...]]:
    """Resolve required imports and serialize only actual selected graphs."""
    if not isinstance(source, PreparedSource):
        raise InputError("OWL profile checking requires an owned PreparedSource")
    if source.union:
        raise ResourceError(
            "OWL profile input requires one physical graph, not a union"
        )
    captured = None
    if supplied is not None:
        if not isinstance(supplied, Mapping):
            raise InputError("Offline OWL imports require a finite mapping")
        captured = {}
        for key, value in supplied.items():
            budget.consume("work")
            budget.consume("states")
            bound_text(key, budget)
            absolute_iri(key)
            if isinstance(value, RdfSource):
                for identity in (value.document_iri, value.effective_url):
                    if identity is not None:
                        bound_text(identity, budget)
            captured[key] = value
    origins = _ImportOrigins(captured, budget) if captured is not None else None
    vocabulary = load_vocabulary(source, imports=origins, budget=budget)
    budget.consume("states")
    original_graphs = [source._read(budget=budget)]
    if origins is not None:
        for scope, handle in vocabulary._source_handles:
            budget.consume("work")
            if source_role(scope) != "imports":
                continue
            origin_document = handle.prepared.document_iri
            if origin_document is None:
                raise ResourceError("Imported document has no original source identity")
            bound_text(origin_document, budget)
            raw = origins.originals.get(str(origin_document))
            if raw is None:
                raise ResourceError("Imported original source ownership is unavailable")
            graph = _original_graph(raw, budget)
            if graph is not None:
                budget.consume("states")
                original_graphs.append(graph)
    if not independent_blank_scopes(original_graphs, budget):
        raise ResourceError(
            "Separately loaded OWL documents share original anonymous term ownership"
        )
    documents: list[ProfileDocument] = []
    entries: list[dict[str, Any]] = []
    aliases: dict[str, int] = {}
    for scope, handle in vocabulary._source_handles:
        if source_role(scope) == "lookup":
            continue
        budget.consume("work")
        budget.consume("states")
        if handle.prepared.literal_fidelity not in ("source_terms", "supplied_terms"):
            raise ResourceError("OWL profile RDF literal preservation is unqualified")
        if handle.prepared.union or handle.union:
            raise ResourceError("Imported OWL documents require one physical graph")
        context = None
        for item in handle.contexts(budget):
            if context is not None:
                raise ResourceError(
                    "OWL documents require an explicit single graph selection"
                )
            context = item
        if context is None:
            raise ResourceError("OWL source graph selection is unavailable")
        kind, name, _ = context
        if name is not None:
            bound_node(name, budget)
        budget.consume("states", 3)
        index = len(documents)
        identifier = "document/" + str(index)
        prepared = handle.prepared
        for identity in (prepared.document_iri, prepared.effective_url):
            if identity is not None:
                bound_text(identity, budget)
                if identity in aliases and aliases[identity] != index:
                    raise ResourceError(
                        "OWL document aliases have conflicting ownership"
                    )
                aliases[identity] = index
        budget.consume("states")
        exported = export_rdf(
            prepared,
            format="turtle",
            graph_name=name,
            budget=budget,
            timeout_seconds=max(0.000001, budget.deadline - time.monotonic()),
        )
        try:
            retain_report(exported.report, budget, issues)
        except LimitExceeded:
            issues.retain_completed(exported.report)
            raise
        reports.append(exported.report)
        if exported.content is None or not exported.report.verified:
            if exported.report.outcome == "exhausted":
                raise LimitExceeded("Selected OWL RDF export exhausted its allowance")
            raise ResourceError("Selected OWL RDF serialization was not qualified")
        path = directory / (str(index) + ".ttl")
        with path.open("xb") as writer:
            writer.write(exported.content)
        document = ProfileDocument(
            identifier,
            prepared.document_iri,
            prepared.effective_url,
            source_role(scope),
            kind,
            name,
            hashlib.sha256(exported.content).hexdigest(),
        )
        documents.append(document)
        entries.append(
            {"id": identifier, "path": str(path), "document_iri": prepared.document_iri}
        )
    if captured is not None:
        for requested, raw in captured.items():
            budget.consume("work")
            if requested in aliases:
                continue
            identities = (
                (raw.document_iri, raw.effective_url)
                if isinstance(raw, RdfSource)
                else (requested,)
            )
            matched = {
                aliases[value]
                for value in identities
                if value is not None and value in aliases
            }
            if len(matched) == 1:
                budget.consume("states")
                aliases[requested] = next(iter(matched))
            elif len(matched) > 1:
                raise ResourceError(
                    "Supplied OWL import aliases join distinct owned documents"
                )
    if not documents:
        raise ResourceError("No selected OWL document was staged")
    return (
        tuple(documents),
        {"documents": entries, "aliases": aliases, "root": 0},
        tuple(reports),
    )
