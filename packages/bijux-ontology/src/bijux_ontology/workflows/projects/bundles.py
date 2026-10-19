"""Portable project resources with explicit logical identities and checked closure."""

import json
from collections.abc import Mapping
from importlib.resources.abc import Traversable
from pathlib import Path
from types import MappingProxyType
from zipfile import ZIP_DEFLATED, ZipFile

from bijux_ontology._json import decode_json, object_value, text_value
from bijux_ontology.documents.archive import bundle_reader, resource_path
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.resources import (
    DocumentLoader,
    OfflineDocumentLoader,
    context_resources,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.vocabulary.view import Vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import check_kind


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
    ) -> None:
        self._configuration = configuration
        self._files = MappingProxyType(dict(files))
        self.rdf: Mapping[str, RdfSource] = MappingProxyType(dict(rdf))
        self.imports: Mapping[str, RdfSource] = MappingProxyType(
            {
                source.document_iri: source
                for source in rdf.values()
                if source.document_iri is not None
            }
        )
        self._contexts = OfflineDocumentLoader(contexts, limits=configuration.limits)
        self.rdf_scope = tuple(rdf)
        self.context_scope = tuple(contexts)
        self.context_documents = context_documents
        self.resource_paths = tuple(files)

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

    def materialize(self, destination: Path) -> ProjectConfig:
        """Write the owned portable files into a new directory without overwrites."""
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
        """Package exactly the owned declared files into a portable ZIP artifact."""
        with (
            destination.open("xb") as handle,
            ZipFile(handle, "w", ZIP_DEFLATED) as archive,
        ):
            for name, payload in self._files.items():
                archive.writestr(resource_path(name), payload)


def load_bundle(
    source: Path | Traversable, *, limits: Limits | None = None
) -> PreparedBundle:
    """Prepare a directory, packaged resource or bounded ZIP project bundle.

    The root ``project.json`` uses the existing project configuration syntax.
    RDF resources require logical ``iri`` bindings so moving physical files cannot
    change their base. Every path is portable and local. Context availability is
    checked for declared entry contexts and actual context-check inputs, without
    claiming all possible future scoped-context applications were exercised.
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
        contexts = {}
        for iri, value in object_value(
            manifest.get("contexts", {}), location="contexts"
        ).items():
            path, payload = member(value)
            bind(iri, path)
            contexts[iri] = decode_json(
                payload,
                source=iri,
                max_bytes=capped.max_bytes,
                max_depth=capped.max_depth,
            )
        context_inputs: list[tuple[str, object, str | None]] = []
        for job in config.checks():
            kind = check_kind(job)
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
    bundle = PreparedBundle(
        config, files, rdf, contexts, tuple(path for path, _, _ in context_inputs)
    )
    resources = context_resources(bundle.document_loader, limits=capped)
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
                document_loader=bundle.document_loader,
                limits=capped,
            )
    return bundle
