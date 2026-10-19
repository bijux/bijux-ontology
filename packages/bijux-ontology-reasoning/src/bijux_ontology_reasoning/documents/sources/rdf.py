"""Exact original RDF document staging shared by explicit native OWL operations."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef

from bijux_ontology.documents.export import export_rdf
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.terms import (
    bound_node,
    bound_text,
    independent_blank_scopes,
)
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.types import GraphKind, source_role
from bijux_ontology.vocabulary.sources.view import Vocabulary

from ...backend.types import ProfileDocument


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


def _capture_rdf_inputs(
    source: PreparedSource,
    supplied: Mapping[str, RdfSource | Payload] | None,
    budget: Operation,
    additional: Sequence[PreparedSource] = (),
) -> tuple[Vocabulary, dict[str, RdfSource | Payload] | None]:
    """Reuse the exact core import selection and original blank ownership guard."""
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
    for companion in additional:
        if not isinstance(companion, PreparedSource) or companion.union:
            raise ResourceError("Additional RDF sources require owned physical graphs")
        budget.consume("states")
        original_graphs.append(companion._read(budget=budget))
    if not independent_blank_scopes(original_graphs, budget):
        raise ResourceError(
            "Separately loaded OWL documents share original anonymous term ownership"
        )
    return vocabulary, captured


def _source_aliases(
    aliases: dict[str, int],
    prepared: PreparedSource,
    index: int,
    budget: Budget,
) -> None:
    """Retain the existing exact document/base alias ownership rules."""
    for identity in (prepared.document_iri, prepared.effective_url):
        if identity is not None:
            bound_text(identity, budget)
            if identity in aliases and aliases[identity] != index:
                raise ResourceError("OWL document aliases have conflicting ownership")
            aliases[identity] = index


def _supplied_aliases(
    captured: dict[str, RdfSource | Payload] | None,
    aliases: dict[str, int],
    budget: Budget,
) -> None:
    """Retain existing selected-resource alias joins without choosing a new source."""
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


def stage_documents(
    source: PreparedSource,
    supplied: Mapping[str, RdfSource | Payload] | None,
    directory: Path,
    budget: Operation,
    issues: IssueCollector,
    reports: list[Report],
) -> tuple[tuple[ProfileDocument, ...], dict[str, Any], tuple[Report, ...]]:
    """Resolve required imports and serialize only actual selected graphs."""
    vocabulary, captured = _capture_rdf_inputs(source, supplied, budget)
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
        _source_aliases(aliases, prepared, index, budget)
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
    _supplied_aliases(captured, aliases, budget)
    if not documents:
        raise ResourceError("No selected OWL document was staged")
    return (
        tuple(documents),
        {"documents": entries, "aliases": aliases, "root": 0},
        tuple(reports),
    )


@dataclass(frozen=True)
class CapturedRdfClosure:
    """One selected closure with immutable import payloads and original provenance.

    Original prepared sources remain separate from serialization snapshots. The
    mapping contains only aliases of actually selected import resources.
    """

    source: PreparedSource
    originals: tuple[PreparedSource, ...]
    imports: Mapping[str, RdfSource]
    reports: tuple[Report, ...]


def capture_rdf_closure(
    source: PreparedSource,
    supplied: Mapping[str, RdfSource | Payload] | None,
    budget: Operation,
    *,
    additional: Sequence[PreparedSource] = (),
    reports: list[Report] | None = None,
) -> CapturedRdfClosure:
    """Snapshot a maintained selected closure once before repeated native work.

    Caller streams are read only by core's first preparation. Snapshot payloads
    are immutable and retain physical graph selection, logical identity and base.
    Additional inputs participate only in original blank-ownership qualification;
    they never add imports to the selected ontology closure.
    """
    if reports is not None and type(reports) is not list:
        raise InputError("Closure report output requires an ordinary report list")
    if isinstance(cast(object, additional), str | bytes) or not isinstance(
        additional, Sequence
    ):
        raise InputError("Additional RDF roles require a finite source sequence")
    budget.consume("states", len(additional))
    budget.consume("bytes", 64 + 16 * len(additional))
    companions_list = []
    for index in range(len(additional)):
        budget.consume("work")
        companion = additional[index]
        if not isinstance(companion, PreparedSource):
            raise InputError("Additional RDF roles require owned prepared sources")
        companions_list.append(companion)
    companions = tuple(companions_list)
    vocabulary, captured = _capture_rdf_inputs(source, supplied, budget, companions)
    size = len(vocabulary._source_handles)
    budget.consume("states", size * 3)
    budget.consume("bytes", 128 + 128 * size)
    handles = tuple(
        handle
        for scope, handle in vocabulary._source_handles
        if source_role(scope) != "lookup"
    )
    aliases: dict[str, int] = {}
    snapshots: dict[int, RdfSource] = {}
    originals: list[PreparedSource] = []
    export_reports: list[Report] = []
    export_issues = IssueCollector(budget)
    for index, handle in enumerate(handles):
        prepared = handle.prepared
        if (
            prepared.union
            or handle.union
            or prepared.literal_fidelity not in ("source_terms", "supplied_terms")
        ):
            raise ResourceError(
                "Captured OWL sources require qualified physical RDF scope"
            )
        context: tuple[GraphKind, URIRef | BNode | None, Graph] | None = None
        for selected in handle.contexts(budget):
            if context is not None:
                raise ResourceError("Captured OWL sources require one physical graph")
            context = selected
        if context is None:
            raise ResourceError("Captured OWL graph selection is unavailable")
        kind, name, selected_graph = context
        if isinstance(name, BNode):
            raise ResourceError(
                "Raw import snapshots cannot rebind a blank graph selector"
            )
        _source_aliases(aliases, prepared, index, budget)
        originals.append(prepared)
        if index == 0:
            continue
        # A graph-selected export is intentionally flattened by export_rdf.
        # Own exactly the selected physical context before dataset serialization.
        budget.consume("states", 4 + len(selected_graph) * 4)
        budget.consume("bytes", 512 + len(selected_graph) * 384)
        budget.consume("triples", len(selected_graph))
        dataset = Dataset()
        target = dataset.default_graph if name is None else dataset.graph(name)
        for triple in selected_graph:
            budget.consume("work")
            for node in triple:
                bound_node(node, budget)
            target.add(triple)
        snapshot = PreparedSource._from_owned(
            dataset,
            is_dataset=kind != "graph",
            document_iri=prepared.document_iri,
            effective_url=prepared.effective_url,
            literal_fidelity=prepared.literal_fidelity,
            max_triples=budget.limits.max_triples,
        )
        budget.consume("states", 2 if reports is not None else 1)
        budget.consume("bytes", 32 if reports is not None else 16)
        exported = export_rdf(
            snapshot,
            format="turtle" if kind == "graph" else "trig",
            budget=budget,
            timeout_seconds=max(0.000001, budget.deadline - time.monotonic()),
        )
        export_reports.append(exported.report)
        if reports is not None:
            reports.append(exported.report)
        try:
            retain_report(exported.report, budget, export_issues)
        except LimitExceeded:
            export_issues.retain_completed(exported.report)
            raise
        if exported.content is None or not exported.report.verified:
            if exported.report.outcome == "exhausted":
                raise LimitExceeded(
                    "Required import snapshot export exhausted its allowance"
                )
            raise ResourceError("Required import snapshot export is unqualified")
        snapshots[index] = RdfSource(
            exported.content,
            format="turtle" if kind == "graph" else "trig",
            document_iri=prepared.document_iri,
            effective_url=prepared.effective_url,
            graph_name=None
            if kind == "graph"
            else "@default"
            if name is None
            else str(name),
            role="import",
        )
    _supplied_aliases(captured, aliases, budget)
    budget.consume("states", len(aliases))
    budget.consume("bytes", 128 + 64 * len(aliases))
    imports = {
        alias: snapshots[index] for alias, index in aliases.items() if index != 0
    }
    return CapturedRdfClosure(
        source, tuple(originals), MappingProxyType(imports), tuple(export_reports)
    )
