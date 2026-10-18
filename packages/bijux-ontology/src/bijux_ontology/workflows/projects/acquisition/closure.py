"""Acquire only dependencies requested by the existing import/context engines."""

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import cast
from urllib.parse import urljoin
from zipfile import ZIP_DEFLATED, ZipFile

from bijux_ontology._json import decode_json, object_value, own_json
from bijux_ontology.documents.archive import resource_path
from bijux_ontology.errors import InputError, ResourceError
from bijux_ontology.jsonld.expansion import expand_document
from bijux_ontology.jsonld.resources import OfflineDocument, OfflineDocumentLoader
from bijux_ontology.jsonld.runtime import processor
from bijux_ontology.rdf.loading import prepare_input
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.vocabulary.loading import load_vocabulary
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import check_kind, input_paths

from .http import HttpAcquisition, _Response
from .types import _Unavailable

_CONTEXT_REL = "http://www.w3.org/ns/json-ld#context"


class Closure(Mapping[str, RdfSource]):
    """Operation-only adapter; ordinary check loaders never acquire a URL."""

    def __init__(
        self,
        manifest: dict[str, object] | None,
        files: dict[str, bytes],
        requests: Mapping[str, str],
        http: HttpAcquisition,
    ) -> None:
        self.manifest, self.files, self.requests, self.http = (
            {} if manifest is None else manifest,
            files,
            requests,
            http,
        )
        self.budget = http.budget
        self.config = (
            None if manifest is None else ProjectConfig(manifest, directory=Path.cwd())
        )
        self.rdf = object_value(
            self.manifest.setdefault("rdf", {}), location="acquisition"
        )
        self.contexts = object_value(
            self.manifest.setdefault("contexts", {}), location="acquisition"
        )
        self.aliases: dict[str, RdfSource] = {}
        self.context_documents: dict[str, object] = {}
        self.context_responses: dict[str, dict[str, object]] = {}

    def __iter__(self) -> Iterator[str]:
        """Expose already-owned RDF aliases without speculating about the web."""
        return iter(self.aliases)

    def __len__(self) -> int:
        """Count registered logical aliases, not possible reachable URLs."""
        return len(self.aliases)

    def __contains__(self, iri: object) -> bool:
        """The maintained import reader alone requests additional RDF resources."""
        if not isinstance(iri, str):
            return False
        self[iri]
        return True

    def _register(self, source: RdfSource) -> None:
        """Reject conflicting concrete aliases before exposing an import binding."""
        for iri in (source.document_iri, source.effective_url):
            if iri is None:
                continue
            bound_text(iri, self.budget)
            previous = self.aliases.get(iri)
            if previous is not None and previous is not source:
                for name in ("format", "effective_url", "graph_name", "union"):
                    if getattr(previous, name) != getattr(source, name):
                        raise InputError(
                            "Conflicting acquired RDF aliases", location=iri
                        )
                assert isinstance(previous.content, bytes) and isinstance(
                    source.content, bytes
                )
                self.budget.consume("work", len(source.content) + 1)
                if previous.content != source.content:
                    raise InputError("Conflicting acquired RDF aliases", location=iri)
            else:
                self.budget.consume("states")
                self.aliases[iri] = source

    def _member(self, path: str) -> bytes:
        """Use supplied bytes or the single explicit request for a declared member."""
        bound_text(path, self.budget)
        resource_path(path)
        if path not in self.files:
            if path not in self.requests:
                raise ResourceError("Missing supplied bundle member", location=path)
            self.files[path] = self.http.get(self.requests[path]).payload
        return self.files[path]

    def _support(self, iri: str, payload: bytes, suffix: str) -> tuple[str, str]:
        """Name a new normal bundle member without deriving paths from URLs."""
        bound_text(iri, self.budget)
        self.budget.consume("work", len(iri) + 1)
        digest = hashlib.sha256(iri.encode()).hexdigest()
        path, alias = "resources/" + digest + "." + suffix, "acquired_" + digest
        self.budget.consume("states", 2)
        if path in self.files and self.files[path] != payload:
            raise InputError(
                "Acquisition support path conflicts with a supplied member"
            )
        self.files[path] = payload
        return path, alias

    def _json_response(self, response: _Response) -> dict[str, object]:
        """Apply maintained Link parsing while PyLD owns all context semantics."""
        try:
            maintained = processor()
        except ResourceError as error:
            raise _Unavailable(str(error)) from error
        item = response.resource
        if item.media_type not in ("application/json", "application/ld+json") and not (
            item.media_type.startswith("application/")
            and item.media_type.endswith("+json")
        ):
            raise InputError("Context retrieval requires a supported JSON media type")
        context_url = None
        if response.link and item.media_type != "application/ld+json":
            self.budget.consume("work", len(response.link) + 1)
            links = maintained.parse_link_header(response.link)
            linked = links.get(_CONTEXT_REL)
            if isinstance(linked, list):
                raise InputError("Multiple HTTP JSON-LD context links are unsupported")
            if linked is not None:
                if not isinstance(linked, dict) or not isinstance(
                    linked.get("target"), str
                ):
                    raise InputError("Malformed HTTP JSON-LD context link")
                context_url = urljoin(item.effective_url, linked["target"])
                self.http.authorize(context_url)
        document = decode_json(
            response.payload,
            source=item.requested_url,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
        )
        aliases = []
        for key, cached in self.http.responses.items():
            self.budget.consume(
                "work", len(cached.resource.effective_url) + len(item.effective_url) + 1
            )
            if (
                cached.payload is response.payload
                and cached.resource.effective_url == item.effective_url
            ):
                self.budget.consume("states")
                aliases.append(key)
        self.budget.consume("states", 2 * len(aliases))
        for key in aliases:
            cached = self.http.responses[key]
            self.http.responses[key] = replace(
                cached, resource=replace(cached.resource, context_url=context_url)
            )
        return {
            "document": document,
            "documentUrl": item.effective_url,
            "contextUrl": context_url,
            "contentType": item.media_type,
        }

    def context(self, url: str, options: dict[str, object]) -> dict[str, object]:
        """Fetch only actual processor requests, preserving effective response bases."""
        bound_text(url, self.budget)
        if url in self.context_responses:
            return self.context_responses[url]
        if url in self.context_documents:
            result = OfflineDocumentLoader(
                {url: self.context_documents[url]}, limits=self.budget.limits
            )(url, options)
        else:
            response = self.http.get(url)
            result = self._json_response(response)
            path, _ = self._support(url, response.payload, "json")
            self.contexts[url] = {
                "path": path,
                "effective_url": result["documentUrl"],
                "context_url": result["contextUrl"],
            }
        self.budget.consume("states")
        self.context_responses[url] = result
        return result

    def __getitem__(self, iri: str) -> RdfSource:
        """Supply exact acquired RDF through the existing required-import protocol."""
        bound_text(iri, self.budget)
        if iri in self.aliases:
            return self.aliases[iri]
        response = self.http.get(iri)
        path, alias = self._support(iri, response.payload, "rdf")
        descriptor = self._rdf_source(response, iri, None)
        if alias in self.rdf:
            raise InputError("Acquisition RDF alias conflicts with supplied project")
        self.rdf[alias] = {
            "path": path,
            "format": descriptor.format,
            "iri": iri,
            "effective_url": descriptor.effective_url,
        }
        self._register(descriptor)
        return descriptor

    def _rdf_source(
        self,
        response: _Response,
        iri: str,
        original: RdfSource | None,
    ) -> RdfSource:
        """Preserve exact graph selection and qualify actual media through loading."""
        syntax = original.format if original is not None else None
        if syntax is not None:
            bound_text(syntax, self.budget)
        media = response.resource.media_type
        if media == "application/json" or media.endswith("+json"):
            remote = self._json_response(response)
            if remote["contextUrl"] is not None:
                raise InputError(
                    "RDF HTTP context links require an explicit supported binding"
                )
            media = "json-ld"
        if media in ("text/plain", "application/octet-stream") and syntax is not None:
            media = syntax
        inferred = prepare_input(
            RdfSource(response.payload, format=media), budget=self.budget
        ).syntax
        if syntax is not None:
            explicit = prepare_input(
                RdfSource(response.payload, format=syntax), budget=self.budget
            ).syntax
            if explicit != inferred:
                raise InputError(
                    "Declared RDF syntax conflicts with acquired media type"
                )
        if (
            original is not None
            and original.effective_url is not None
            and (original.effective_url != response.resource.effective_url)
        ):
            raise InputError("Acquired effective URL differs from its explicit binding")
        return RdfSource(
            response.payload,
            format=inferred,
            document_iri=iri,
            effective_url=response.resource.effective_url,
            graph_name=original.graph_name if original is not None else None,
            union=original.union if original is not None else False,
        )

    def prepare(self) -> bytes:
        """Qualify selected closures, then encode the existing portable archive form."""
        if self.config is None:
            raise InputError("Bundle preparation requires a project manifest")
        selected: list[RdfSource] = []
        for name, original in self.config.rdf.items():
            self.budget.consume("work")
            entry = object_value(self.rdf[name], location="acquisition")
            path = cast(str, entry["path"])
            payload = self._member(path)
            if path in self.requests:
                response = self.http.get(self.requests[path])
                if original.document_iri is None:
                    raise InputError("Acquired RDF requires an explicit logical IRI")
                source = self._rdf_source(response, original.document_iri, original)
                entry["format"], entry["effective_url"] = (
                    source.format,
                    source.effective_url,
                )
            else:
                syntax = original.format or Path(path).suffix.removeprefix(".")
                source = replace(original, content=payload, format=syntax)
            self._register(source)
            selected.append(source)
        for iri, raw in tuple(self.contexts.items()):
            self.budget.consume("work")
            entry = (
                {"path": raw}
                if isinstance(raw, str)
                else object_value(raw, location="acquisition")
            )
            path = cast(str, entry["path"])
            payload = self._member(path)
            if path in self.requests:
                result = self._json_response(self.http.get(self.requests[path]))
                for field, observed in (
                    ("effective_url", result["documentUrl"]),
                    ("context_url", result["contextUrl"]),
                ):
                    if (
                        field in entry
                        and entry[field] is not None
                        and entry[field] != observed
                    ):
                        raise InputError(
                            "Acquired context metadata conflicts with explicit binding"
                        )
                    entry[field] = observed
                self.contexts[iri] = entry
                self.context_responses[iri] = result
            else:
                document = decode_json(
                    payload,
                    max_bytes=self.budget.limits.max_bytes,
                    max_depth=self.budget.limits.max_depth,
                )
                metadata = self.config.context_metadata[iri]
                self.context_documents[iri] = OfflineDocument(
                    document, metadata.effective_url, metadata.context_url
                )
        for source in selected:
            load_vocabulary(
                source, imports=self, document_loader=self.context, budget=self.budget
            )
        if self.config.contexts:
            try:
                processor()
            except ResourceError as error:
                raise _Unavailable(str(error)) from error
        for iri in tuple(self.config.contexts):
            expand_document(
                {"@context": iri}, document_loader=self.context, budget=self.budget
            )
        for job in self.config.checks():
            kind = check_kind(job)
            for _, value in input_paths(job):
                self._member(resource_path(value))
            if kind == "context":
                members = job.get("documents", [job])
                if not isinstance(members, list):
                    raise InputError("Context documents require an array")
                for raw in members:
                    item = object_value(raw, location="acquisition")
                    if "document" not in item:
                        continue
                    path = resource_path(item["document"])
                    document = decode_json(
                        self._member(path),
                        max_bytes=self.budget.limits.max_bytes,
                        max_depth=self.budget.limits.max_depth,
                    )
                    expand_document(
                        document,
                        base=cast(str | None, item.get("base", job.get("base"))),
                        document_loader=self.context,
                        budget=self.budget,
                    )
        owned = own_json(
            self.manifest,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
        )
        payload = (json.dumps(owned, ensure_ascii=False, indent=2) + "\n").encode()
        if len(payload) > self.budget.limits.max_bytes:
            raise InputError("Acquired project manifest exceeds max_bytes")
        self.budget.consume("bytes", len(payload))
        self.files["project.json"] = payload
        self.budget.consume("work", sum(len(value) for value in self.files.values()))
        buffer = BytesIO()
        with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
            for name, content in self.files.items():
                self.budget.consume("work")
                archive.writestr(resource_path(name), content)
        archive_bytes = buffer.getvalue()
        self.budget.consume("bytes", len(archive_bytes))
        return archive_bytes
