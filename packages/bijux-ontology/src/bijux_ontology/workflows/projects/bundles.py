"""Portable project resources with explicit logical identities and checked closure."""

import json
from collections.abc import Mapping, Sequence
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from zipfile import ZIP_DEFLATED, ZipFile

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.archive import bundle_reader, resource_path
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import decode_json, object_value, text_value
from bijux_ontology.jsonld.resources import (
    DocumentLoader,
    DocumentLocation,
    OfflineDocument,
    OfflineDocumentLoader,
    context_resources,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import check_kind, input_paths


class PreparedBundle:
    """Owned files and logical RDF/context bindings for a portable project.

    ``rdf_scope`` names resources whose selected import closures were checked.
    ``context_scope`` names processed entry contexts; ``context_documents`` names
    actual processed inputs. These establish resource availability for that
    scope, not semantic acceptance or coverage of every future scoped-context
    application. Runtime checks retain their own acceptance contracts.
    """

    def __init__(
        self,
        configuration: ProjectConfig,
        files: Mapping[str, bytes],
        rdf: Mapping[str, RdfSource],
        contexts: Mapping[str, object],
        context_documents: tuple[str, ...],
        *,
        imports: Mapping[str, RdfSource] | None = None,
        context_payloads: Mapping[str, bytes] | None = None,
        context_metadata: Mapping[str, DocumentLocation] | None = None,
        context_scope: tuple[str, ...] | None = None,
        has_dependencies: bool = False,
    ) -> None:
        self._configuration = configuration
        self._files = MappingProxyType(dict(files))
        self.rdf: Mapping[str, RdfSource] = MappingProxyType(dict(rdf))
        self.imports: Mapping[str, RdfSource] = MappingProxyType(
            dict(imports)
            if imports is not None
            else {
                source.document_iri: source
                for source in rdf.values()
                if source.document_iri is not None
            }
        )
        self._context_payloads = MappingProxyType(dict(context_payloads or {}))
        self._context_metadata = MappingProxyType(dict(context_metadata or {}))
        self._has_dependencies = has_dependencies
        self._contexts = OfflineDocumentLoader(contexts, limits=configuration.limits)
        self.rdf_scope = tuple(rdf)
        self.context_scope = tuple(contexts) if context_scope is None else context_scope
        self.context_documents = context_documents
        self.resource_paths = tuple(files)

    @property
    def configuration(self) -> ProjectConfig:
        """Inspect a fresh configuration without changing this owned bundle."""
        return ProjectConfig(
            self._configuration.to_dict(),
            directory=self._configuration.directory,
            source=self._configuration.source,
        )

    @property
    def files(self) -> Mapping[str, bytes]:
        """Inspect immutable owned payloads without acquiring external inputs."""
        return self._files

    @property
    def document_loader(self) -> DocumentLoader:
        """Resolve only this bundle's owned JSON documents with logical bases."""
        return self._contexts

    def vocabulary(self, name: str, *, lookup: tuple[str, ...] = ()) -> Vocabulary:
        """Load a named selected vocabulary using only supplied bundle resources."""
        if {name, *lookup} - self.rdf.keys():
            raise InputError("Unknown bundle RDF resource")
        return load_vocabulary(
            self.rdf[name],
            imports=self.imports,
            lookup=tuple(self.rdf[item] for item in lookup),
            document_loader=self.document_loader,
            limits=self._configuration.limits,
        )

    def materialize(
        self, destination: Path, *, local_only: bool = False
    ) -> ProjectConfig:
        """Write local owned files, explicitly acknowledging external dependencies."""
        if type(local_only) is not bool:
            raise InputError("Local-only materialization requires a boolean")
        if self._has_dependencies and not local_only:
            raise InputError(
                "Dependent bundles require explicit local_only materialization"
            )
        destination = destination.resolve()
        if destination.exists():
            raise InputError("Bundle destination must be a new directory")
        destination.mkdir(parents=True, exist_ok=False)
        for name, payload in self._files.items():
            target = destination.joinpath(*resource_path(name).split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(payload)
        return ProjectConfig(
            self._configuration.to_dict(),
            directory=destination,
            source=str(destination / "project.json"),
        )

    def write_archive(self, destination: Path) -> None:
        """Package a locally closed bundle without hiding external dependencies."""
        if self._has_dependencies:
            raise InputError("A dependent bundle is not a standalone archive")
        with (
            destination.open("xb") as handle,
            ZipFile(handle, "w", ZIP_DEFLATED) as archive,
        ):
            for name, payload in self._files.items():
                archive.writestr(resource_path(name), payload)


class _Availability:
    """Own finite logical bindings independently of package-local filenames."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.imports: dict[str, RdfSource] = {}
        self.contexts: dict[str, bytes] = {}
        self.context_metadata: dict[str, DocumentLocation] = {}

    def payload(self, iri: str, payload: object) -> bytes:
        """Bound logical metadata and raw bytes before comparing or retaining."""
        bound_text(iri, self.budget)
        absolute_iri(iri)
        if not isinstance(payload, bytes):
            raise InputError("Bundle dependencies require owned byte payloads")
        if len(payload) > self.budget.limits.max_bytes:
            raise LimitExceeded("Dependency payload exceeds max_bytes")
        return payload

    def equal_bytes(self, left: bytes, right: bytes) -> bool:
        """Pay for actual content comparison without rehashing shared snapshots."""
        if left is right:
            return True
        if len(left) != len(right):
            return False
        self.budget.consume("work", len(left))
        return left == right

    def dual_role(
        self, iri: str, source: RdfSource, payload: bytes, metadata: DocumentLocation
    ) -> None:
        """Allow only exact bytes and the same effective base across RDF/JSON roles."""
        if (
            source.compression is not None
            or (source.effective_url or source.document_iri)
            != (metadata.effective_url or iri)
            or metadata.context_url is not None
            or not isinstance(source.content, bytes)
            or not self.equal_bytes(source.content, payload)
        ):
            raise InputError("Conflicting bundle logical binding", location=iri)

    def retain(self, payload: bytes) -> None:
        """Charge a new logical resource before retaining its immutable payload."""
        self.budget.consume("resources")
        self.budget.consume("states")
        self.budget.consume("bytes", len(payload))

    def rdf(self, iri: str, source: RdfSource) -> None:
        """Keep exact syntax, bases and graph selection when registering an alias."""
        if not isinstance(source, RdfSource):
            raise InputError("Bundle dependencies require RDF descriptors")
        payload = self.payload(iri, source.content)
        for value in (
            source.format,
            source.document_iri,
            source.effective_url,
            source.graph_name,
            source.compression,
        ):
            if value is not None:
                bound_text(value, self.budget)
        if iri not in (source.document_iri, source.effective_url):
            raise InputError("Dependency RDF aliases require exact document identities")
        if iri in self.contexts:
            self.dual_role(iri, source, self.contexts[iri], self.context_metadata[iri])
        before = self.imports.get(iri)
        if before is not None:
            fields = (
                "format",
                "document_iri",
                "effective_url",
                "graph_name",
                "union",
                "compression",
            )
            if any(
                getattr(before, field) != getattr(source, field) for field in fields
            ):
                raise InputError("Conflicting bundle logical binding", location=iri)
            assert isinstance(before.content, bytes)
            if not self.equal_bytes(before.content, payload):
                raise InputError("Conflicting bundle logical binding", location=iri)
            return
        self.retain(payload)
        self.imports[iri] = source

    def context(
        self, iri: str, raw: bytes, metadata: DocumentLocation | None = None
    ) -> None:
        """Retain exact context payloads by their logical document identity."""
        payload = self.payload(iri, raw)
        metadata = metadata or DocumentLocation()
        for value in (metadata.effective_url, metadata.context_url):
            if value is not None:
                bound_text(value, self.budget)
                absolute_iri(value)
        if iri in self.imports:
            self.dual_role(iri, self.imports[iri], payload, metadata)
        if iri in self.contexts:
            before = self.context_metadata[iri]
            if (
                (before.effective_url or iri) != (metadata.effective_url or iri)
                or before.context_url != metadata.context_url
                or not self.equal_bytes(self.contexts[iri], payload)
            ):
                raise InputError("Conflicting bundle logical binding", location=iri)
            return
        self.retain(payload)
        self.contexts[iri] = payload
        self.context_metadata[iri] = metadata


def load_bundle(
    source: Path | Traversable,
    *,
    dependencies: Sequence[PreparedBundle] = (),
    limits: Limits | None = None,
) -> PreparedBundle:
    """Prepare a directory, packaged resource or bounded ZIP project bundle.

    The root ``project.json`` uses the existing project configuration syntax.
    RDF resources require logical ``iri`` bindings so moving physical files cannot
    change their base. Every path is portable and local. Context availability is
    checked for declared entry contexts and actual context-check inputs, without
    claiming all possible future scoped-context applications were exercised.
    Dependencies contribute owned offline resources, never inherited jobs or policy.
    Local files/configuration remain separate from the combined availability closure.
    """
    selected = limits or Limits()
    with bundle_reader(source, limits=selected) as read:
        manifest = object_value(
            decode_json(
                read("project.json"),
                source="project.json",
                max_bytes=selected.max_bytes,
                max_depth=selected.max_depth,
            ),
            location="project.json",
        )
        config = ProjectConfig(manifest, directory=Path.cwd(), source="project.json")
        capped = Limits(
            **{
                name: min(getattr(selected, name), getattr(config.limits, name))
                for name in Limits.__dataclass_fields__
            }
        )
        manifest["limits"] = {
            name: getattr(capped, name) for name in Limits.__dataclass_fields__
        }
        config = ProjectConfig(manifest, directory=Path.cwd(), source="project.json")
        files = {
            "project.json": (
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            ).encode()
        }
        if len(files["project.json"]) > capped.max_bytes:
            raise LimitExceeded("Bundle manifest exceeds max_bytes")
        ownership = Budget(capped)
        ownership.consume("resources")
        ownership.consume("bytes", len(files["project.json"]))
        supplied_dependencies: object = dependencies
        if not isinstance(supplied_dependencies, Sequence) or isinstance(
            supplied_dependencies, str | bytes
        ):
            raise InputError("Bundle dependencies require a finite typed sequence")
        dependency_count = len(dependencies)
        ownership.consume("states", dependency_count)
        ownership.consume("resources", dependency_count)
        ownership.consume("bytes", 64 + 16 * dependency_count)
        availability = _Availability(ownership)
        for index in range(dependency_count):
            ownership.consume("work")
            try:
                dependency = dependencies[index]
            except IndexError as error:
                raise InputError(
                    "Bundle dependency sequence changed during capture"
                ) from error
            if not isinstance(dependency, PreparedBundle):
                raise InputError("Bundle dependencies require prepared bundles")
            ownership.consume(
                "states", len(dependency.imports) + len(dependency._context_payloads)
            )
            ownership.consume("states", len(dependency.context_scope))
            for context_iri in dependency.context_scope:
                ownership.consume("work")
                bound_text(context_iri, ownership)
                if context_iri not in dependency._context_payloads:
                    raise InputError("Dependency contexts require owned raw snapshots")
            for dependency_iri, descriptor in dependency.imports.items():
                ownership.consume("work")
                availability.rdf(dependency_iri, descriptor)
            for dependency_iri, raw in dependency._context_payloads.items():
                ownership.consume("work")
                availability.context(
                    dependency_iri,
                    raw,
                    dependency._context_metadata.get(
                        dependency_iri, DocumentLocation()
                    ),
                )
        rdf = {}
        logical: dict[str, str] = {}
        rdf_bindings: dict[str, tuple[str, str | None, str | None, bool]] = {}

        def member(value: object) -> tuple[str, bytes]:
            """Own each declared portable input once."""
            name = resource_path(value)
            if name not in files:
                payload = read(name)
                if len(payload) > capped.max_bytes:
                    raise LimitExceeded(
                        "Bundle member exceeds max_bytes", location=name
                    )
                ownership.consume("resources")
                ownership.consume("bytes", len(payload))
                files[name] = payload
            return name, files[name]

        def bind(iri: str, name: str) -> None:
            """Reject logical aliases that identify different physical resources."""
            if iri in logical and logical[iri] != name:
                raise InputError("Conflicting bundle logical binding", location=iri)
            logical[iri] = name

        raw_rdf = object_value(manifest.get("rdf", {}), location="rdf")
        for name, descriptor in config.rdf.items():
            entry = object_value(raw_rdf[name], location=f"rdf/{name}")
            path, payload = member(entry.get("path"))
            if descriptor.document_iri is None:
                raise InputError(
                    "Bundle RDF resources require logical iri bindings", location=name
                )
            syntax = descriptor.format or Path(path).suffix.removeprefix(".")
            selection = (path, syntax, descriptor.graph_name, descriptor.union)
            for iri in (descriptor.document_iri, descriptor.effective_url):
                if iri is None:
                    continue
                bind(iri, path)
                if iri in rdf_bindings and rdf_bindings[iri] != selection:
                    raise InputError("Conflicting bundle RDF selection", location=iri)
                rdf_bindings[iri] = selection
            rdf[name] = RdfSource(
                payload,
                format=syntax,
                document_iri=descriptor.document_iri,
                effective_url=descriptor.effective_url,
                graph_name=descriptor.graph_name,
                union=descriptor.union,
            )
        for descriptor in rdf.values():
            for iri in (descriptor.document_iri, descriptor.effective_url):
                if iri is not None:
                    availability.rdf(iri, descriptor)
        local_contexts = {}
        for iri, value in object_value(
            manifest.get("contexts", {}), location="contexts"
        ).items():
            context_path = value.get("path") if isinstance(value, dict) else value
            path, payload = member(context_path)
            bind(iri, path)
            availability.context(iri, payload, config.context_metadata[iri])
            local_contexts[iri] = payload
        context_inputs: list[tuple[str, object, str | None]] = []
        for job in config.checks():
            kind = check_kind(job)
            for _, value in input_paths(job):
                member(value)
            members = job.get("documents", [])
            if not isinstance(members, list):
                raise InputError("Context batch documents must be an array")
            jobs = [
                job,
                *(object_value(item, location="documents") for item in members),
            ]
            for item in jobs:
                for key in ("inventory", "declarations", "document", "expectations"):
                    if key not in item or kind not in (
                        "vocabulary",
                        "models",
                        "context",
                    ):
                        continue
                    path, payload = member(item[key])
                    if kind == "context" and key == "document":
                        base = item.get("base", job.get("base"))
                        if base is not None:
                            base = text_value(base, location="base")
                        context_inputs.append(
                            (
                                path,
                                decode_json(
                                    payload,
                                    source=path,
                                    max_bytes=capped.max_bytes,
                                    max_depth=capped.max_depth,
                                ),
                                base,
                            )
                        )
    contexts = {
        iri: OfflineDocument(
            decode_json(
                payload,
                source=iri,
                max_bytes=capped.max_bytes,
                max_depth=capped.max_depth,
            ),
            effective_url=availability.context_metadata[iri].effective_url,
            context_url=availability.context_metadata[iri].context_url,
        )
        for iri, payload in availability.contexts.items()
    }
    bundle = PreparedBundle(
        config,
        files,
        rdf,
        contexts,
        tuple(path for path, _, _ in context_inputs),
        imports=availability.imports,
        context_payloads=availability.contexts,
        context_metadata=availability.context_metadata,
        context_scope=tuple(local_contexts),
        has_dependencies=dependency_count > 0,
    )
    resources = context_resources(bundle.document_loader, budget=ownership)
    for name in bundle.rdf_scope:
        load_vocabulary(
            bundle.rdf[name],
            imports=bundle.imports,
            document_loader=resources,
            limits=capped,
        )
    if contexts or context_inputs:
        from bijux_ontology.jsonld.expansion import expand_document

        for iri in contexts:
            expand_document({"@context": iri}, document_loader=resources, limits=capped)
        for _, document, base in context_inputs:
            expand_document(
                document,
                base=base,
                document_loader=resources,
                limits=capped,
            )
    return bundle
