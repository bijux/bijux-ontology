"""Operation-owned RDF snapshots, vocabulary views and explicit context resources."""

from collections.abc import Iterator, Mapping

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import (
    DocumentLoader,
    OfflineDocumentLoader,
    context_document_loader,
)
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig


class _Imports(Mapping[str, RdfSource]):
    def __init__(self, prepared: "ProjectResources") -> None:
        self._prepared = prepared
        self._names = {
            source.document_iri: name
            for name, source in prepared.configuration.rdf.items()
            if source.document_iri is not None
        }

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
    """Reuse owned sources and indexes within one explicit project execution.

    Resource reads remain lazy. A new instance is required to observe file
    changes after first use; preparation budgets are shared across its reads.
    """

    def __init__(self, configuration: ProjectConfig) -> None:
        self.configuration = configuration
        self._vocabularies: dict[
            tuple[str, tuple[str, ...], str | None, bool], Vocabulary
        ] = {}
        self._sources: dict[str, PreparedSource] = {}
        self._preparation = Budget(configuration.limits)
        self._imports = _Imports(self)
        self._contexts = context_document_loader(
            OfflineDocumentLoader(
                configuration.contexts, max_bytes=configuration.limits.max_bytes
            ),
            budget=self._preparation,
        )

    def vocabulary(
        self,
        name: str,
        *,
        lookup: tuple[str, ...] = (),
        graph_name: str | None = None,
        union: bool = False,
    ) -> Vocabulary:
        """Prepare one selected source/import closure and reuse its owned indexes."""
        key = (name, lookup, graph_name, union)
        if key not in self._vocabularies:
            config = self.configuration
            missing = set((name, *lookup)) - set(config.rdf)
            if missing:
                raise InputError(f"Unknown RDF resources: {', '.join(sorted(missing))}")
            if len(self._vocabularies) >= config.limits.max_resources:
                raise LimitExceeded("Exceeded prepared vocabulary view max_resources")
            local = self.source(name)
            lookups = tuple(self.source(item).graph() for item in lookup)
            self._vocabularies[key] = load_vocabulary(
                RdfSource(
                    local.graph(graph_name, union=union),
                    document_iri=local.document_iri,
                    effective_url=local.effective_url,
                ),
                imports=self._imports,
                lookup=lookups,
                limits=config.limits,
            )
        return self._vocabularies[key]

    def source(self, name: str) -> PreparedSource:
        """Reuse one named owned RDF snapshot across explicit validation roles."""
        if name not in self.configuration.rdf:
            raise InputError("Unknown RDF resource", location=name)
        if name not in self._sources:
            self._sources[name] = prepare_source(
                self.configuration.rdf[name],
                limits=self.configuration.limits,
                budget=self._preparation,
                document_loader=self._contexts,
            )
        return self._sources[name]

    @property
    def document_loader(self) -> DocumentLoader:
        """Use the operation's explicit offline context resources and budget."""
        return self._contexts

    @property
    def imports(self) -> Mapping[str, RdfSource]:
        """Resolve only registered logical import IRIs through lazy snapshots."""
        return self._imports
