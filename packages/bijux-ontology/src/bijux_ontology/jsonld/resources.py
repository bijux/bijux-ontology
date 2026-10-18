"""Owned offline JSON-LD resources and per-operation resolution policy."""

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import cast
from urllib.parse import urljoin

from bijux_ontology._json import decode_json, own_json
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text

from ..errors import InputError, LimitExceeded, attach_input_failure

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


class OfflineDocumentLoader:
    """Resolve only supplied documents, retaining their logical document URLs.

    Values are parsed JSON documents or paths to UTF-8 JSON snapshots. A URL
    alias needing a distinct effective URL can use a caller-supplied loader.
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
        for url, document in documents.items():
            self._documents[url] = (
                document
                if isinstance(document, Path)
                else own_json(
                    document,
                    source=url,
                    max_bytes=self._max_bytes,
                    max_depth=self._limits.max_depth,
                )
            )

    def __call__(self, url: str, options: dict[str, object]) -> dict[str, object]:
        """Return the PyLD loader response without invoking a network loader."""
        metadata = Budget(replace(self._limits, max_bytes=self._max_bytes))
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
                        payload = handle.read(self._max_bytes + 1)
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
            )
            return {"contextUrl": None, "documentUrl": url, "document": document}
        except InputError as error:
            attach_input_failure(error, document_iri=url, location=location)
            raise


def _absolutise_imports(context: object, base: str) -> None:
    """Bind imports only inside actual context and scoped-context positions."""
    # PyLD resolves remote @context URLs but leaves relative @import URLs
    # tied to the application base. Resolve those against the supplying document.
    if isinstance(context, dict):
        mapping = cast(dict[str, object], context)
        imported = mapping.get("@import")
        if isinstance(imported, str):
            mapping["@import"] = urljoin(base, imported)
        for definition in mapping.values():
            if isinstance(definition, dict):
                _absolutise_imports(definition.get("@context"), base)
    elif isinstance(context, list):
        for child in cast(list[object], context):
            _absolutise_imports(child, base)


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
    Separate instances never share caches, even when they use the same loader.
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
        self._logical: dict[str, str] = {}

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

    def __call__(self, url: str, options: dict[str, object]) -> dict[str, object]:
        """Own effective resources without changing the supplying loader's data."""
        if url in self._snapshots:
            _consume_work(self._snapshots[url]["document"], self.budget)
            return deepcopy(self._snapshots[url])
        self.budget.consume("resources")
        response = self._loader(url, options).copy()
        effective_url = response.get("documentUrl")
        if not isinstance(effective_url, str):
            raise ContextInputError(
                "A loader response must supply its effective documentUrl"
            )
        document = own_json(
            response.get("document"),
            source=url,
            max_bytes=self.budget.limits.max_bytes,
            max_depth=self.budget.limits.max_depth,
        )
        self.budget.consume("bytes", _json_size(document))
        _consume_work(document, self.budget)
        response["document"] = document
        nodes = document if isinstance(document, list) else [document]
        for node in nodes:
            if isinstance(node, dict):
                context = node.get("@context")
                self._unscoped = self._unscoped and context_is_unscoped(context)
                _absolutise_imports(context, effective_url)
        absolute_iri(effective_url)
        context_url = response.get("contextUrl")
        if context_url is not None and not isinstance(context_url, str):
            raise ContextInputError("A loader contextUrl must be a string or None")
        if isinstance(context_url, str):
            context_url = urljoin(effective_url, context_url)
            absolute_iri(context_url)
            response["contextUrl"] = context_url
        else:
            response["contextUrl"] = None
        identity = json.dumps(
            {"document": document, "contextUrl": context_url},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if effective_url in self._logical and self._logical[effective_url] != identity:
            raise ContextInputError(
                f"Conflicting supplied JSON-LD resources identify {effective_url}"
            )
        self._logical[effective_url] = identity
        self._snapshots[url] = response
        return deepcopy(response)


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
