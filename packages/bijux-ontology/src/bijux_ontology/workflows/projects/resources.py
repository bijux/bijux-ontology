"""Operation-owned RDF snapshots, vocabulary views and explicit context resources."""

import json
import os
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from rdflib import ConjunctiveGraph, Dataset, Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.values import _decode_json, own_json
from bijux_ontology.jsonld.resources import (
    ContextResources,
    DocumentLoader,
    DocumentLocation,
    OfflineDocument,
    OfflineDocumentLoader,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.loading import prepare_input
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig


def _read_snapshot(path: Path, budget: Budget) -> bytes:
    """Prepay the over-limit sentinel and account unusable physical reads."""
    budget.consume("bytes")
    budget.consume("work")
    cap = min(
        budget.limits.max_bytes, budget.remaining("bytes"), budget.remaining("work")
    )
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Project inputs require regular files")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            value = handle.read(cap + 1)
    finally:
        os.close(descriptor)
    budget.consume("bytes", max(0, len(value) - 1))
    budget.consume("work", max(0, len(value) - 1))
    if len(value) > cap and cap < budget.limits.max_bytes:
        raise LimitExceeded("Project capture exhausted its shared allowance")
    return value


class _CapturedFiles(Mapping[Path, bytes | Exception]):
    """Operation-owned lazy reads from immutable private capture descriptors."""

    def __init__(self, entries: Mapping[Path, Path | Exception], budget: Budget):
        self.budget = budget
        owned: dict[Path, Path | Exception] = {}
        for original, private in entries.items():
            budget.consume("work")
            budget.consume("states", 2)
            budget.consume("bytes", 32)
            bound_text(str(original), budget)
            if isinstance(private, Path):
                bound_text(str(private), budget)
            owned[original] = private
        self._entries = MappingProxyType(owned)
        self._cache: dict[Path, bytes | Exception] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[Path]:
        return iter(self._entries)

    def __getitem__(self, path: Path) -> bytes | Exception:
        self.budget.consume("work")
        if path in self._cache:
            return self._cache[path]
        private = self._entries[path]
        if isinstance(private, Exception):
            value: bytes | Exception = private
        else:
            self.budget.consume("resources")
            try:
                value = _read_snapshot(private, self.budget)
            except OSError:
                value = InputError(
                    "Project input could not be read", location=str(path)
                )
            if isinstance(value, bytes) and len(value) > self.budget.limits.max_bytes:
                value = LimitExceeded("Captured project input exceeds max_bytes")
        self._cache[path] = value
        return value


def _captured_bytes(path: Path, captured: Mapping[Path, bytes | Exception]) -> bytes:
    """Read an explicitly captured path without falling back to mutable files."""
    try:
        value = captured[path]
    except KeyError:
        raise InputError("Input was not captured", location=str(path)) from None
    if isinstance(value, Exception):
        raise value
    if not isinstance(value, bytes):
        raise InputError("Captured inputs require bytes or acquisition failures")
    return value


class _ContextDocuments:
    """Retain immutable raw documents independently of operation context caches."""

    def __init__(
        self,
        inputs: Mapping[str, Path | str],
        storage: Budget,
        metadata: Mapping[str, DocumentLocation] | None = None,
        captured: Mapping[Path, bytes | Exception] | None = None,
    ) -> None:
        if len(inputs) > storage.limits.max_resources:
            raise LimitExceeded("Too many supplied context documents")
        self.captured = captured
        self.inputs = dict(inputs)
        self.metadata = dict(metadata or {})
        self.storage = storage
        for document in self.inputs.values():
            if isinstance(document, str):
                storage.consume("resources")
                storage.consume("bytes", len(document.encode()))

    def acquire(self, url: str, document: object) -> None:
        """Own one bounded replacement or lazily resolved original document."""
        if isinstance(document, OfflineDocument):
            self.metadata[url] = DocumentLocation(
                document.effective_url, document.context_url
            )
            document = document.document
        location = self.metadata.get(url, DocumentLocation())
        for value in (location.effective_url, location.context_url):
            if value is not None:
                bound_text(value, self.storage)
                absolute_iri(value)
        if isinstance(document, Path):
            if self.captured is None:
                document = OfflineDocumentLoader(
                    {url: document}, limits=self.storage.limits
                )(url, {})["document"]
            else:
                payload = _captured_bytes(document, self.captured)
                document = _decode_json(
                    payload,
                    source=str(document),
                    max_bytes=self.storage.limits.max_bytes,
                    max_depth=self.storage.limits.max_depth,
                    budget=self.storage,
                )
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
        metadata = self.metadata.get(url, DocumentLocation())
        return {
            "documentUrl": metadata.effective_url or url,
            "contextUrl": metadata.context_url,
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
        self._names: dict[str, str] = {}
        for name, identities in prepared._registered.items():
            for iri in identities:
                if iri is None:
                    continue
                bound_text(iri, prepared._preparation)
                previous = self._names.get(iri)
                if previous is not None and previous != name:
                    raise InputError("RDF resource logical IRIs must be unique")
                if previous is None:
                    prepared._preparation.consume("states")
                    self._names[iri] = name

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

    def __init__(
        self,
        configuration: ProjectConfig,
        *,
        budget: Budget | None = None,
        captured: Mapping[Path, bytes | Exception] | None = None,
    ) -> None:
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Project resources require a Budget")
        if budget is not None and budget.limits != configuration.limits:
            raise InputError("Project limits conflict with the shared budget")
        self.configuration = configuration
        self._budget = budget
        self._captured: Mapping[Path, bytes | Exception] | None = None
        if isinstance(captured, _CapturedFiles):
            if budget is not captured.budget:
                raise InputError("Captured inputs belong to another operation budget")
            self._captured = captured
        elif captured is not None:
            if not isinstance(captured, Mapping):
                raise InputError("Captured project inputs require a mapping")
            owner = budget if budget is not None else Budget(configuration.limits)
            owned_capture: dict[Path, bytes | Exception] = {}
            self._captured = owned_capture
            for path, value in captured.items():
                owner.consume("work")
                owner.consume("states")
                owner.consume("bytes", 16)
                if not isinstance(path, Path) or not isinstance(
                    value, bytes | Exception
                ):
                    raise InputError(
                        "Captured paths require immutable bytes or failures"
                    )
                owned_capture[path] = value
        self._lock = RLock()
        self._rdf = dict(configuration.rdf)
        self._registered = {
            name: (source.document_iri, source.effective_url)
            for name, source in self._rdf.items()
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
        self._preparation = (
            budget if budget is not None else Budget(configuration.limits)
        )
        self._storage = budget if budget is not None else Budget(configuration.limits)
        self._documents = _ContextDocuments(
            configuration.contexts,
            self._storage,
            configuration.context_metadata,
            self._captured,
        )
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
                        budget=self._budget,
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
            descriptor = resource
            if self._captured is not None and isinstance(resource.content, Path):
                document = resource.document_iri or resource.content.as_uri()
                descriptor = replace(
                    resource,
                    document_iri=document,
                    effective_url=resource.effective_url or document,
                )
            before = prepare_input(descriptor, budget=self._storage)
            if self._captured is not None and isinstance(resource.content, Path):
                # Resolve syntax/base/compression from the original descriptor;
                # only its physical byte acquisition changes ownership.
                before = replace(
                    before,
                    resource=replace(
                        before.resource,
                        content=_captured_bytes(resource.content, self._captured),
                    ),
                )
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
                compression=None,
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

    def read_json(self, path: Path) -> object:
        """Decode one original-path snapshot using this operation's owned inputs."""
        if self._budget is not None:
            bound_text(str(path), self._budget)
        if self._captured is None:
            with path.open("rb") as handle:
                payload = handle.read(self._storage.limits.max_bytes + 1)
        else:
            payload = _captured_bytes(path, self._captured)
        return _decode_json(
            payload,
            source=str(path),
            max_bytes=self._storage.limits.max_bytes,
            max_depth=self._storage.limits.max_depth,
            budget=self._budget,
        )

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
                name: (item.document_iri, item.effective_url)
                for name, item in rdf.items()
            }
            revised._imports = _Imports(revised)
            retained = {
                url: value
                for url, value in self._documents.inputs.items()
                if url not in contexts
            }
            revised._documents = _ContextDocuments(
                retained,
                revised._storage,
                self._documents.metadata,
            )
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

    def context_loader(self, budget: Budget) -> DocumentLoader:
        """Reuse owned documents with fresh processor caches for one operation."""

        def load(url: str, options: dict[str, object]) -> dict[str, object]:
            """Acquire raw first-use snapshots under the revision's existing lock."""
            with self.operation():
                return self._documents(url, options)

        return ContextResources(load, budget=budget)

    @property
    def imports(self) -> Mapping[str, RdfSource]:
        """Resolve only registered logical import IRIs through lazy snapshots."""
        return self._imports
