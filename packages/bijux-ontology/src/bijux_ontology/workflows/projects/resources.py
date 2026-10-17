"""Operation-owned RDF snapshots, vocabulary views and explicit context resources."""

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock

from rdflib import ConjunctiveGraph, Dataset, Graph

from bijux_ontology._json import own_json
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import (
    ContextResources,
    DocumentLoader,
    OfflineDocumentLoader,
)
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.loading import prepare_input
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig


class _ContextDocuments:
    """Retain immutable raw documents independently of operation context caches."""

    def __init__(self, inputs: Mapping[str, Path | str], storage: Budget) -> None:
        if len(inputs) > storage.limits.max_resources:
            raise LimitExceeded("Too many supplied context documents")
        self.inputs = dict(inputs)
        self.storage = storage
        for document in self.inputs.values():
            if isinstance(document, str):
                storage.consume("resources")
                storage.consume("bytes", len(document.encode()))

    def acquire(self, url: str, document: object) -> None:
        """Own one bounded replacement or lazily resolved original document."""
        if isinstance(document, Path):
            document = OfflineDocumentLoader(
                {url: document}, limits=self.storage.limits
            )(url, {})["document"]
        owned = own_json(
            document,
            source=url,
            max_bytes=self.storage.limits.max_bytes,
            max_depth=self.storage.limits.max_depth,
        )
        encoded = json.dumps(owned, ensure_ascii=False, allow_nan=False)
        self.storage.consume("resources")
        self.storage.consume("bytes", len(encoded.encode()))
        self.inputs[url] = encoded

    def __call__(self, url: str, options: dict[str, object]) -> dict[str, object]:
        if url not in self.inputs:
            raise InputError("No supplied JSON-LD context", location=url)
        if isinstance(self.inputs[url], Path):
            self.acquire(url, self.inputs[url])
        return {
            "documentUrl": url,
            "contextUrl": None,
            "document": json.loads(str(self.inputs[url])),
        }


class _Contexts(ContextResources):
    def __init__(self, owner: "ProjectResources") -> None:
        self._owner = owner
        super().__init__(owner._documents, budget=owner._preparation)

    def __call__(self, url: str, options: dict[str, object]) -> dict[str, object]:
        with self._owner.operation():
            if self._owner._preparing is not None:
                self._owner._context_sources.add(self._owner._preparing)
            return super().__call__(url, options)


class _Imports(Mapping[str, RdfSource]):
    def __init__(self, prepared: "ProjectResources") -> None:
        self._prepared = prepared
        self._names = {
            iri: name for name, iri in prepared._registered.items() if iri is not None
        }
        if len(self._names) != sum(
            iri is not None for iri in prepared._registered.values()
        ):
            raise InputError("RDF resource logical IRIs must be unique")

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)

    def __contains__(self, key: object) -> bool:
        return key in self._names

    def __getitem__(self, iri: str) -> RdfSource:
        prepared = self._prepared.source(self._names[iri])
        return RdfSource(
            prepared.graph(),
            document_iri=prepared.document_iri,
            effective_url=prepared.effective_url,
        )


class ProjectResources:
    """Reuse first-use snapshots and replace resources through independent revisions.

    Unread original paths remain lazy. Replacement descriptors/documents are
    acquired before returning a revision. Already acquired resources never reread
    their paths, including when a changed context requires fresh RDF preparation.
    """

    def __init__(self, configuration: ProjectConfig) -> None:
        self.configuration = configuration
        self._lock = RLock()
        self._rdf = dict(configuration.rdf)
        self._registered = {
            name: source.document_iri for name, source in self._rdf.items()
        }
        self._vocabularies: dict[
            tuple[str, tuple[str, ...], str | None, bool], Vocabulary
        ] = {}
        self._dependencies: dict[
            tuple[str, tuple[str, ...], str | None, bool], frozenset[str]
        ] = {}
        self._loading: set[str] | None = None
        self._sources: dict[str, PreparedSource] = {}
        self._sizes: dict[str, tuple[int, int]] = {}
        self._context_sources: set[str] = set()
        self._preparing: str | None = None
        self._preparation = Budget(configuration.limits)
        self._storage = Budget(configuration.limits)
        self._documents = _ContextDocuments(configuration.contexts, self._storage)
        self._imports = _Imports(self)
        self._contexts = _Contexts(self)

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Keep one revision's acquisition and dependency records atomic."""
        with self._lock:
            yield

    def vocabulary(
        self,
        name: str,
        *,
        lookup: tuple[str, ...] = (),
        graph_name: str | None = None,
        union: bool = False,
    ) -> Vocabulary:
        """Prepare one selected source/import closure and reuse its owned indexes."""
        with self.operation():
            key = (name, lookup, graph_name, union)
            if key not in self._vocabularies:
                config = self.configuration
                missing = {name, *lookup} - set(self._rdf)
                if missing:
                    raise InputError(
                        f"Unknown RDF resources: {', '.join(sorted(missing))}"
                    )
                if len(self._vocabularies) >= config.limits.max_resources:
                    raise LimitExceeded(
                        "Exceeded prepared vocabulary view max_resources"
                    )
                previous = self._loading
                self._loading = set()
                try:
                    local = self.source(name)
                    lookups = tuple(self.source(item).graph() for item in lookup)
                    vocabulary = load_vocabulary(
                        RdfSource(
                            local.graph(graph_name, union=union),
                            document_iri=local.document_iri,
                            effective_url=local.effective_url,
                        ),
                        imports=self._imports,
                        lookup=lookups,
                        limits=config.limits,
                    )
                    self._dependencies[key] = frozenset(self._loading)
                    self._vocabularies[key] = vocabulary
                finally:
                    self._loading = previous
            return self._vocabularies[key]

    def _acquire(self, name: str) -> None:
        """Own source bytes or graph terms before recording reusable preparation."""
        resource = self._rdf[name]
        if name not in self._sizes:
            before = prepare_input(resource, budget=self._storage)
            content: Graph | bytes
            if isinstance(resource.content, Graph):
                owned = prepare_source(resource, budget=self._preparation)
                content = owned.dataset() if owned.is_dataset else owned.graph()
                records = (
                    content.quads()
                    if isinstance(content, Dataset | ConjunctiveGraph)
                    else content
                )
                size, triples = 0, 0
                for record in records:
                    triples += 1
                    size += sum(len(str(term).encode()) for term in record)
                self._storage.consume("bytes", size)
                self._storage.consume("triples", triples)
                self._sources[name] = owned
            else:
                content = before.read()
                size, triples = len(content), 0
            self._rdf[name] = replace(
                resource,
                content=content,
                format=before.syntax,
                document_iri=before.document_iri,
                effective_url=before.effective_url,
            )
            self._sizes[name] = (size, triples)

    def source(self, name: str) -> PreparedSource:
        """Reuse one named owned RDF snapshot across explicit validation roles."""
        with self.operation():
            if name not in self._rdf:
                raise InputError("Unknown RDF resource", location=name)
            if self._loading is not None:
                self._loading.add(name)
            if name not in self._sources:
                self._acquire(name)
                if name not in self._sources:
                    previous = self._preparing
                    self._preparing = name
                    try:
                        prepared = prepare_source(
                            self._rdf[name],
                            budget=self._preparation,
                            document_loader=self._contexts,
                        )
                        triples = prepared._quad_count()
                        size, accounted = self._sizes[name]
                        self._storage.consume("triples", max(0, triples - accounted))
                        self._sizes[name] = (size, triples)
                        self._sources[name] = prepared
                    finally:
                        self._preparing = previous
            return self._sources[name]

    def replace_resources(
        self,
        *,
        rdf: Mapping[str, RdfSource],
        contexts: Mapping[str, object],
    ) -> "ProjectResources":
        """Acquire explicit replacements, reuse proven independent preparation."""
        with self.operation():
            if set(rdf) - set(self._rdf) or set(contexts) - set(self._documents.inputs):
                raise InputError("Replacement keys must select registered resources")
            if any(not isinstance(value, RdfSource) for value in rdf.values()):
                raise InputError("RDF replacements require RdfSource descriptors")
            revised = ProjectResources(self.configuration)
            revised._rdf = self._rdf | dict(rdf)
            revised._registered = self._registered | {
                name: item.document_iri for name, item in rdf.items()
            }
            revised._imports = _Imports(revised)
            retained = {
                url: value
                for url, value in self._documents.inputs.items()
                if url not in contexts
            }
            revised._documents = _ContextDocuments(retained, revised._storage)
            for url, document in contexts.items():
                revised._documents.acquire(url, document)
            revised._contexts = _Contexts(revised)
            affected = set(rdf)
            if contexts:
                affected.update(self._context_sources)
            revised._context_sources = self._context_sources - set(rdf)
            revised._sizes = {
                name: (size, 0 if name in affected else triples)
                for name, (size, triples) in self._sizes.items()
                if name not in rdf
            }
            for size, triples in revised._sizes.values():
                revised._storage.consume("resources")
                revised._storage.consume("bytes", size)
                revised._storage.consume("triples", triples)
            revised._sources = {
                name: source
                for name, source in self._sources.items()
                if name not in affected
            }
            aliases_changed = revised._registered != self._registered
            for key, vocabulary in self._vocabularies.items():
                dependencies = self._dependencies.get(key)
                if (
                    not aliases_changed
                    and dependencies is not None
                    and not dependencies.intersection(affected)
                ):
                    revised._vocabularies[key] = vocabulary
                    revised._dependencies[key] = dependencies
            for name in rdf:
                revised.source(name)
            return revised

    @property
    def document_loader(self) -> DocumentLoader:
        """Use the operation's explicit offline context resources and budget."""
        return self._contexts

    @property
    def imports(self) -> Mapping[str, RdfSource]:
        """Resolve only registered logical import IRIs through lazy snapshots."""
        return self._imports
