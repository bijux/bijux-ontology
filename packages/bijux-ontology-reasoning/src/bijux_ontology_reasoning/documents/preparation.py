"""Bounded capture and explicit offline aliases for original native documents."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from rdflib import BNode
from rdflib import Literal as RdfLiteral

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text

from .types import OwlDocument, OwlSyntax

SYNTAXES = ("owlxml", "functional", "manchester", "obo")


def native_syntax(value: object) -> OwlSyntax:
    """Accept only the finite explicitly selected maintained native formats."""
    if type(value) is not str or value not in SYNTAXES:
        raise InputError("Choose owlxml, functional, manchester or obo")
    return cast(OwlSyntax, value)


def native_iri(value: object, operation: Budget) -> str:
    """Bound named source identities before URI validation and dictionary use."""
    if not isinstance(value, str) or isinstance(value, (BNode, RdfLiteral)):
        raise InputError("A native document identity must be an absolute named IRI")
    bound_text(value, operation)
    return str(absolute_iri(value))


def _content(value: bytes, operation: Budget) -> None:
    if len(value) > operation.limits.max_bytes:
        raise LimitExceeded("Native document exceeds max_bytes")
    operation.consume("work", len(value) + 1)
    operation.consume("bytes", len(value))


def prepare_owl_document(
    content: Path | str | bytes,
    *,
    format: OwlSyntax,
    document_iri: str,
    effective_url: str | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OwlDocument:
    """Own original bytes; strings are content and no URL is retrieved.

    The effective URL is the actual parser base, including for imported native
    documents. No parsing or semantic completeness is claimed by this factory.
    """
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Native document limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    syntax = native_syntax(format)
    identity = native_iri(document_iri, operation)
    effective = native_iri(
        identity if effective_url is None else effective_url, operation
    )
    operation.consume("resources")
    if isinstance(content, Path):
        bound_text(str(content), operation)
        try:
            allowance = min(operation.limits.max_bytes, operation.remaining("bytes"))
            with content.open("rb") as reader:
                raw = reader.read(allowance + 1)
            if len(raw) > allowance:
                raise LimitExceeded("Native input exceeds its remaining byte allowance")
        except OSError as error:
            raise ResourceError("Native input file is unavailable") from error
    elif isinstance(content, bytes):
        raw = content
    elif type(content) is str:
        bound_text(content, operation)
        raw = content.encode("utf-8")
    else:
        raise InputError("Native document content requires Path, str or bytes")
    _content(raw, operation)
    if syntax != "owlxml":
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InputError("Native text syntax requires UTF-8 bytes") from error
    return OwlDocument._owned(
        content=raw,
        format=syntax,
        document_iri=identity,
        effective_url=effective,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def stage_native_documents(
    source: OwlDocument,
    imports: Mapping[str, OwlDocument] | None,
    directory: Path,
    operation: Budget,
) -> tuple[tuple[OwlDocument, ...], dict[str, Any]]:
    """Capture a finite exact alias map; OWLAPI discovers the required closure."""
    documents: list[OwlDocument] = []
    records: list[dict[str, object]] = []
    aliases: dict[str, int] = {}
    effective: dict[str, int] = {}

    def add(document: OwlDocument, alias: str | None) -> None:
        """Stage each effective resource once and retain its explicit aliases."""
        if not isinstance(document, OwlDocument):
            raise InputError("Imports require prepared native OwlDocument records")
        identity = native_iri(document.document_iri, operation)
        base = native_iri(document.effective_url, operation)
        native_syntax(document.format)
        if type(document.content) is not bytes or type(document.sha256) is not str:
            raise InputError("Invalid prepared native document")
        bound_text(document.sha256, operation)
        _content(document.content, operation)
        if hashlib.sha256(document.content).hexdigest() != document.sha256:
            raise InputError("Prepared native document content differs from its hash")
        index = effective.get(base)
        if index is not None:
            prior = documents[index]
            if prior.content != document.content or prior.format != document.format:
                raise InputError("Conflicting native resources share an effective URL")
        else:
            operation.consume("resources")
            operation.consume("states", 3)
            index = len(documents)
            path = directory / f"document-{index}.owl"
            bound_text(str(path), operation)
            with path.open("xb") as writer:
                writer.write(document.content)
            documents.append(document)
            records.append(
                {"path": str(path), "effective_url": base, "format": document.format}
            )
            effective[base] = index
        for key in (identity, base, alias):
            if key is None:
                continue
            qualified = native_iri(key, operation)
            operation.consume("work")
            if qualified in aliases and aliases[qualified] != index:
                raise InputError("Conflicting native import aliases")
            if qualified not in aliases:
                operation.consume("states")
                aliases[qualified] = index

    add(source, None)
    if imports is not None:
        if not isinstance(imports, Mapping):
            raise InputError("Native imports require an explicit finite mapping")
        size = len(imports)
        operation.consume("resources", size)
        operation.consume("states", size)
        seen = 0
        for alias, document in imports.items():
            operation.consume("work")
            if seen >= size:
                raise InputError("Native import mapping changed during capture")
            seen += 1
            add(document, native_iri(alias, operation))
        if seen != size:
            raise InputError("Native import mapping changed during capture")
    return tuple(documents), {"documents": records, "aliases": aliases}
