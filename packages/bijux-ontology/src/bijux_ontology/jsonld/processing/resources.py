"""Owned offline JSON-LD resources and per-operation resolution policy."""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from urllib.parse import urljoin

from bijux_ontology.diagnostics.errors import (
    InputError,
    LimitExceeded,
    attach_input_failure,
)
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.json.values import (
    _capture_json,
    _measure_json,
    decode_json,
    own_json,
)
from bijux_ontology.rdf.identifiers import absolute_iri

DocumentLoader = Callable[[str, dict[str, object]], dict[str, object]]


class ContextInputError(InputError):
    """A required JSON-LD input could not be processed under the supplied policy."""


def _json_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def _consume_work(value: object, budget: Budget) -> None:
    stack = [value]
    while stack:
        budget.consume("work")
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


@dataclass(frozen=True)
class DocumentLocation:
    """Explicit remote response identities retained independently of raw JSON."""

    effective_url: str | None = None
    context_url: str | None = None


@dataclass(frozen=True)
class OfflineDocument:
    """A supplied JSON document or path with explicit response metadata."""

    document: object
    effective_url: str | None = None
    context_url: str | None = None


class OfflineDocumentLoader:
    """Resolve only supplied documents, retaining their logical document URLs.

    Values are parsed JSON documents, paths to UTF-8 JSON snapshots, or explicit
    OfflineDocument records retaining effective URLs and context-link metadata.
    """

    def __init__(
        self,
        documents: Mapping[str, object],
        *,
        max_bytes: int | None = None,
        limits: Limits | None = None,
    ) -> None:
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
            raise ContextInputError("max_bytes must be positive")
        self._limits = limits or Limits(max_bytes=max_bytes or 5_000_000)
        self._max_bytes = min(
            max_bytes or self._limits.max_bytes, self._limits.max_bytes
        )
        if len(documents) > self._limits.max_resources:
            raise LimitExceeded("Too many supplied context documents")
        self._documents = {}
        self._locations = {}
        metadata = Budget(self._limits)
        for url, document in documents.items():
            bound_text(url, metadata)
            absolute_iri(url)
            location = DocumentLocation()
            if isinstance(document, OfflineDocument):
                location = DocumentLocation(
                    document.effective_url, document.context_url
                )
                document = document.document
            for value in (location.effective_url, location.context_url):
                if value is not None:
                    bound_text(value, metadata)
                    absolute_iri(value)
            self._locations[url] = location
            self._documents[url] = (
                document
                if isinstance(document, Path)
                else own_json(
                    document,
                    source=url,
                    max_bytes=self._max_bytes,
                    max_depth=self._limits.max_depth,
                    budget=metadata,
                )
            )

    def __call__(
        self, url: str, options: dict[str, object], *, budget: Budget | None = None
    ) -> dict[str, object]:
        """Return offline content under an optional enclosing capture allowance."""
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Offline JSON-LD loading requires a Budget")
        metadata = (
            budget
            if budget is not None
            else Budget(replace(self._limits, max_bytes=self._max_bytes))
        )
        bound_text(url, metadata)
        supplied = self._documents.get(url)
        location = str(supplied) if isinstance(supplied, Path) else ""
        bound_text(location, metadata)
        try:
            if url not in self._documents:
                raise ContextInputError(f"No supplied JSON-LD document for {url}")
            document = self._documents[url]
            if isinstance(document, Path):
                try:
                    with document.open("rb") as handle:
                        size = os.fstat(handle.fileno()).st_size
                        if size > min(self._max_bytes, metadata.limits.max_bytes):
                            raise LimitExceeded("JSON-LD document exceeds max_bytes")
                        # Prepay the maintained decoder and its ownership copy;
                        # read only the bounded observed snapshot plus a growth marker.
                        metadata.consume("work", size + 1)
                        metadata.consume("bytes", 128 * (size + 1))
                        payload = handle.read(size + 1)
                    if len(payload) != size:
                        raise ContextInputError(
                            "JSON-LD document changed during reading"
                        )
                    if len(payload) > self._max_bytes:
                        raise LimitExceeded(
                            f"JSON-LD document exceeds byte limit: {url}"
                        )
                except (OSError, UnicodeError) as error:
                    raise ContextInputError(
                        f"Cannot load JSON-LD document {url}: {error}"
                    ) from error
                try:
                    document = decode_json(
                        payload,
                        source=url,
                        max_bytes=self._max_bytes,
                        max_depth=self._limits.max_depth,
                    )
                except LimitExceeded:
                    raise
                except (ValueError, RecursionError) as error:
                    raise ContextInputError(
                        f"Cannot load JSON-LD document {url}: {error}"
                    ) from error
            document = own_json(
                document,
                source=url,
                max_bytes=self._max_bytes,
                max_depth=self._limits.max_depth,
                budget=metadata,
            )
            location_metadata = self._locations[url]
            return {
                "contextUrl": location_metadata.context_url,
                "documentUrl": location_metadata.effective_url or url,
                "document": document,
            }
        except InputError as error:
            attach_input_failure(error, document_iri=url, location=location)
            raise


def _resource_join(base: str, reference: str, budget: Budget) -> str:
    """Reserve bounded URL resolution workspace before constructing a result."""
    allowance = 2 * (len(base) + len(reference)) + 32
    if allowance > budget.limits.max_bytes:
        raise LimitExceeded("Resolved resource URL exceeds max_bytes")
    budget.consume("work", allowance)
    budget.consume("bytes", allowance * 4)
    result = urljoin(base, reference)
    bound_text(result, budget)
    return result


def _absolutise_imports(context: object, base: str, budget: Budget) -> bool:
    """Bind imports only inside actual context and scoped-context positions."""
    # PyLD resolves remote @context URLs but leaves relative @import URLs
    # tied to the application base. Resolve those against the supplying document.
    changed = False
    if isinstance(context, dict):
        mapping = cast(dict[str, object], context)
        imported = mapping.get("@import")
        if isinstance(imported, str):
            absolute = _resource_join(base, imported, budget)
            changed |= absolute != imported
            mapping["@import"] = absolute
        for definition in mapping.values():
            if isinstance(definition, dict):
                changed |= _absolutise_imports(definition.get("@context"), base, budget)
    elif isinstance(context, list):
        for child in cast(list[object], context):
            changed |= _absolutise_imports(child, base, budget)
    return changed


def context_is_unscoped(context: object) -> bool:
    """Prove absence of scope-changing syntax without interpreting term mappings.

    Remote references are checked separately from the actual resources loaded.
    This is only an optimisation precondition; public expansion owns semantics.
    """
    if context is None or isinstance(context, str):
        return True
    if isinstance(context, list):
        return all(context_is_unscoped(item) for item in context)
    if not isinstance(context, dict):
        return False
    for name, definition in context.items():
        if name == "@propagate":
            return False
        if name.startswith("@"):
            continue
        if isinstance(definition, str) and definition.startswith("@"):
            return False
        if isinstance(definition, dict) and (
            "@context" in definition
            or isinstance(definition.get("@id"), str)
            and definition["@id"].startswith("@")
        ):
            return False
    return True


class ContextResources:
    """Own resource snapshots, metadata and one cumulative operation budget.

    Responses are copied at entry and exit; a URL is read once per operation.
    Reuse through ``context_resources`` preserves that snapshot and its budget.
    Independent instances never share caches. Explicit budget forks reuse their
    parent's captured responses and qualify independent child-owned copies.
    """

    def __init__(
        self,
        document_loader: DocumentLoader | None = None,
        *,
        budget: Budget | None = None,
    ) -> None:
        if isinstance(document_loader, ContextResources):
            raise InputError("Reuse context_resources instead of nesting operations")
        self._budget = budget or Budget()
        self._loader = (
            document_loader
            if document_loader is not None
            else OfflineDocumentLoader({}, limits=self.budget.limits)
        )
        self._snapshots: dict[str, dict[str, object]] = {}
        self._unscoped = True
        self._logical: dict[str, dict[str, object]] = {}

    @property
    def budget(self) -> Budget:
        """The operation's fixed budget, shared by every consumer of its resources."""
        return self._budget

    def effective_url(self, requested: str) -> str | None:
        """Inspect the effective identity of a resource already owned here."""
        response = self._snapshots.get(requested)
        value = response.get("documentUrl") if response is not None else None
        return value if isinstance(value, str) else None

    def unscoped(self) -> bool:
        """Whether every resource used so far lacks scope-changing syntax."""
        return self._unscoped

    def for_budget(self, budget: Budget) -> "ContextResources":
        """Qualify per-record copies while preserving the cumulative resource owner."""
        if not isinstance(budget, Budget):
            raise InputError("Resource forks require an operation Budget")
        if budget is self.budget:
            return self
        return ContextResources(self._owned_response, budget=budget)

    def __call__(self, url: str, options: dict[str, object]) -> dict[str, object]:
        """Return an independent copy of this operation's qualified response."""
        return cast(
            dict[str, object],
            own_json(
                self._owned_response(url, options),
                source=url,
                max_bytes=self.budget.limits.max_bytes,
                max_depth=self.budget.limits.max_depth,
                budget=self.budget,
            ),
        )

    def _owned_response(
        self, url: str, options: dict[str, object]
    ) -> dict[str, object]:
        """Hand owned data only to a qualifying child or public copy boundary."""
        bound_text(url, self.budget)
        if url in self._snapshots:
            return self._snapshots[url]
        self.budget.consume("resources")
        supplied = (
            self._loader(url, options, budget=self.budget)
            if isinstance(self._loader, OfflineDocumentLoader)
            else self._loader(url, options)
        )
        raw, size = _capture_json(
            supplied,
            source=url,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
            budget=self.budget,
        )
        # The processor's semantic input allowance is independent of immutable
        # scalar sharing in the ownership copy.
        self.budget.consume("bytes", size)
        if not isinstance(raw, dict):
            raise ContextInputError("A loader response must be a JSON object")
        response = cast(dict[str, object], raw)
        effective_url = response.get("documentUrl")
        if not isinstance(effective_url, str):
            raise ContextInputError(
                "A loader response must supply its effective documentUrl"
            )
        bound_text(effective_url, self.budget)
        document = response.get("document")
        changed = "document" not in response
        response["document"] = document
        nodes = document if isinstance(document, list) else [document]
        for node in nodes:
            if isinstance(node, dict):
                context = node.get("@context")
                self._unscoped = self._unscoped and context_is_unscoped(context)
                changed |= _absolutise_imports(context, effective_url, self.budget)
        absolute_iri(effective_url)
        context_url = response.get("contextUrl")
        changed |= "contextUrl" not in response
        if context_url is not None and not isinstance(context_url, str):
            raise ContextInputError("A loader contextUrl must be a string or None")
        if isinstance(context_url, str):
            absolute_context = _resource_join(effective_url, context_url, self.budget)
            changed |= absolute_context != context_url
            context_url = absolute_context
            absolute_iri(context_url)
            response["contextUrl"] = context_url
        else:
            response["contextUrl"] = None
        if changed:
            _measure_json(
                response,
                source=url,
                max_bytes=self.budget.limits.max_bytes,
                max_depth=self.budget.limits.max_depth,
                budget=self.budget,
            )
        previous = self._logical.get(effective_url)
        if previous is not None and self._identity(previous, url) != self._identity(
            response, url
        ):
            raise ContextInputError(
                f"Conflicting supplied JSON-LD resources identify {effective_url}"
            )
        self._logical[effective_url] = response
        self._snapshots[url] = response
        return response

    def _identity(self, response: dict[str, object], source: str) -> str:
        """Compare colliding logical identities without copying owned documents."""
        identity_input = {
            "document": response.get("document"),
            "contextUrl": response.get("contextUrl"),
        }
        measurement = _measure_json(
            identity_input,
            source=source,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
            budget=self.budget,
        )
        # ASCII output retains one-byte characters; Unicode may require four.
        # Reserve encoder slack and a full result copy before serialization.
        width = 4 if measurement.ascii_only else 8
        self.budget.reserve_storage(128 + width * measurement.size)
        return json.dumps(
            identity_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def context_resources(
    document_loader: DocumentLoader | None = None,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextResources:
    """Acquire an operation's concrete resources without rebinding its budget.

    An owned loader inherits its original budget unless the exact same budget is
    supplied. Equal limits do not make a different budget the same operation.
    Plain caller loaders start an independent snapshot and remain unmodified.
    """
    if isinstance(document_loader, ContextResources):
        if budget is not None and budget is not document_loader.budget:
            raise InputError("Context resources belong to a different operation budget")
        operation = document_loader.budget
        if limits is not None and limits != operation.limits:
            raise InputError("Explicit limits differ from the operation budget")
        return document_loader
    if budget is not None and limits is not None and limits != budget.limits:
        raise InputError("Explicit limits differ from the operation budget")
    return ContextResources(document_loader, budget=budget or Budget(limits))


def context_document_loader(
    document_loader: DocumentLoader | None = None,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> DocumentLoader:
    """Own a stable offline-by-default loader for one caller-visible operation.

    Reuse this policy when a consumer invokes another PyLD processing operation.
    Custom caller failures propagate unchanged; no global processor state changes.
    """
    return context_resources(document_loader, limits=limits, budget=budget)
