"""Bounded native RDF loading and operation-owned source preparation."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO
from typing import Literal as TypingLiteral

from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, Literal, URIRef

from bijux_ontology.errors import (
    InputError,
    LimitExceeded,
    ResourceError,
    attach_input_failure,
)
from bijux_ontology.limits import Budget, Limits

from .compression import decode_gzip
from .identifiers import absolute_iri, language_tag
from .parsing import RDF_FORMATS, parse_dataset
from .snapshots import PreparedSource, bind_namespaces, copy_dataset, copy_graph
from .sources import Compression, Payload, RdfSource
from .terms import bound_text

_ALIASES = {
    "ttl": "turtle",
    "application/x-turtle": "turtle",
    "text/turtle": "turtle",
    "n-triples": "nt",
    "application/n-triples": "nt",
    "rdfxml": "xml",
    "rdf": "xml",
    "rdf/xml": "xml",
    "application/rdf+xml": "xml",
    "application/trig": "trig",
    "nq": "nquads",
    "n-quads": "nquads",
    "application/n-quads": "nquads",
    "jsonld": "json-ld",
    "application/ld+json": "json-ld",
}


def _format(source: RdfSource) -> str:
    supplied = source.format
    if supplied is None and source.compression is not None:
        raise InputError("Compressed RDF requires an explicit format")
    if supplied is None and isinstance(source.content, Path):
        supplied = source.content.suffix.removeprefix(".")
    if not supplied:
        raise InputError("RDF bytes, text and streams require an explicit format")
    supplied = _ALIASES.get(supplied.lower(), supplied.lower())
    if supplied not in RDF_FORMATS and supplied != "json-ld":
        raise InputError(f"Unsupported RDF format: {supplied}")
    return supplied


def _stream_bytes(
    stream: IO[str] | IO[bytes],
    max_bytes: int,
    budget: Budget,
    *,
    binary: bool,
) -> bytes:
    """Read bounded chunks through EOF, including valid short-read streams."""
    payload = bytearray()
    while True:
        budget.consume("work")
        size = min(65_536, max_bytes + 1 - len(payload))
        try:
            data = stream.read(size)
        except (OSError, ValueError) as error:
            raise ResourceError("Cannot read RDF source stream") from error
        except TypeError as error:
            raise InputError("RDF stream must support bounded read(size)") from error
        if not isinstance(data, str | bytes) or binary and not isinstance(data, bytes):
            raise InputError(
                "Compressed RDF streams must return bytes"
                if binary
                else "RDF stream must return text or bytes"
            )
        if not data:
            return bytes(payload)
        if len(data) > size:
            raise LimitExceeded("RDF stream exceeded its bounded read size")
        try:
            chunk = data.encode("utf-8") if isinstance(data, str) else data
        except UnicodeError as error:
            raise InputError("RDF stream text is not valid UTF-8 content") from error
        if len(payload) + len(chunk) > max_bytes:
            raise LimitExceeded(
                "Compressed RDF exceeds max_compressed_bytes"
                if binary
                else "RDF resource exceeds max_bytes"
            )
        budget.consume("bytes", len(chunk))
        payload.extend(chunk)


def _read(content: Payload, max_bytes: int, budget: Budget, *, binary: bool) -> bytes:
    """Own finite source bytes without closing caller-provided streams."""
    if isinstance(content, Path):
        try:
            with content.open("rb") as stream:
                return _stream_bytes(stream, max_bytes, budget, binary=binary)
        except OSError as error:
            raise ResourceError(f"Cannot read RDF source: {content}") from error
    if isinstance(content, bytes):
        payload = content
    elif isinstance(content, str):
        if binary:
            raise InputError("Compressed RDF requires binary content")
        if len(content) > max_bytes:
            raise LimitExceeded("RDF text exceeds max_bytes")
        try:
            payload = content.encode("utf-8")
        except UnicodeError as error:
            raise InputError("RDF text is not valid UTF-8 content") from error
    elif isinstance(content, Graph):
        raise InputError("Parsed graphs are not byte streams")
    else:
        return _stream_bytes(content, max_bytes, budget, binary=binary)
    if len(payload) > max_bytes:
        raise LimitExceeded(
            "Compressed RDF exceeds max_compressed_bytes"
            if binary
            else "RDF resource exceeds max_bytes"
        )
    budget.consume("bytes", len(payload))
    return payload


def _charge_graph(source: Graph, budget: Budget, max_bytes: int) -> None:
    """Bound supplied graph terms and literal metadata before owned copying."""
    size = 0

    def charge(text: str) -> None:
        """Reserve exact UTF-8 storage before metadata validation or ownership."""
        nonlocal size
        if len(text) > max_bytes:
            raise LimitExceeded("RDF term metadata exceeds max_bytes")
        amount = len(text.encode("utf-8"))
        size += amount
        if size > max_bytes:
            raise LimitExceeded("RDF graph term text exceeds max_bytes")
        budget.consume("bytes", amount)

    records = (
        source.quads() if isinstance(source, Dataset | ConjunctiveGraph) else source
    )
    for record in records:
        budget.consume("triples")
        for term in record:
            if isinstance(term, Graph):
                term = term.identifier
            if not isinstance(term, URIRef | BNode | Literal):
                raise InputError("Unsupported RDF term shape")
            charge(term)
            if isinstance(term, URIRef):
                absolute_iri(term)
            if isinstance(term, Literal):
                if term.datatype is not None:
                    charge(term.datatype)
                    absolute_iri(term.datatype)
                if term.language is not None:
                    charge(term.language)
                    language_tag(term.language)


@dataclass(frozen=True)
class SourcePreparation:
    """Resolved source identity and one operation's already-charged resource scope."""

    resource: RdfSource
    document_iri: str | None
    effective_url: str | None
    syntax: str | None
    limits: Limits
    budget: Budget
    preserve_literals: bool
    require_complete_graphs: bool
    max_compressed_bytes: int

    def read(self) -> bytes:
        """Read RDF bytes; compressed and expanded bytes share the operation budget."""
        compressed = self.resource.compression is not None
        payload = _read(
            self.resource.content,
            self.max_compressed_bytes if compressed else self.limits.max_bytes,
            self.budget,
            binary=compressed,
        )
        return (
            decode_gzip(payload, max_bytes=self.limits.max_bytes, budget=self.budget)
            if compressed
            else payload
        )


def prepare_input(
    source: RdfSource | Payload,
    *,
    format: str | None = None,
    base: str | None = None,
    compression: Compression | None = None,
    max_compressed_bytes: int | None = None,
    max_bytes: int | None = None,
    max_triples: int | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    require_complete_graphs: bool = False,
) -> SourcePreparation:
    """Resolve one source and budget without reading or parsing its content.

    max_bytes bounds expanded RDF. max_compressed_bytes independently bounds
    gzip input and defaults to max_bytes. Both representations consume the same
    cumulative byte budget; compression is never inferred from content or names.
    """
    if type(require_complete_graphs) is not bool:
        raise InputError("require_complete_graphs must be a boolean")
    if type(preserve_literals) is not bool:
        raise InputError("preserve_literals must be a boolean")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    selected = (
        limits
        if limits is not None
        else (budget.limits if budget is not None else Limits())
    )
    if max_bytes is not None:
        selected = replace(selected, max_bytes=max_bytes)
    if max_triples is not None:
        selected = replace(selected, max_triples=max_triples)
    compressed_cap = (
        selected.max_bytes if max_compressed_bytes is None else max_compressed_bytes
    )
    if type(compressed_cap) is not int or compressed_cap <= 0:
        raise InputError("max_compressed_bytes must be a positive integer")
    operation = budget if budget is not None else Budget(selected)
    max_bytes = selected.max_bytes
    max_triples = selected.max_triples
    operation.consume("resources")
    resource = (
        source
        if isinstance(source, RdfSource)
        else RdfSource(source, format, base, compression=compression)
    )
    if isinstance(source, RdfSource):
        if compression is not None and resource.compression not in (None, compression):
            raise InputError("Conflicting RDF source compression declarations")
        if format is not None and resource.format not in (None, format):
            raise InputError("Conflicting RDF source format declarations")
        if base is not None and resource.document_iri not in (None, base):
            raise InputError("Conflicting RDF source identities")
        resource = replace(
            resource,
            format=resource.format or format,
            compression=resource.compression or compression,
            document_iri=resource.document_iri or base,
        )
    document = resource.document_iri
    if document is None and isinstance(resource.content, Path):
        document = resource.content.resolve().as_uri()
    document = absolute_iri(document) if document is not None else None
    effective = resource.effective_url or document
    if effective is not None:
        absolute_iri(effective)
    for text in (
        document or "",
        effective or "",
        resource.location,
        resource.role if resource.role != "input" else "",
    ):
        if text:
            bound_text(text, operation)
    try:
        syntax = None if isinstance(resource.content, Graph) else _format(resource)
    except InputError as error:
        attach_input_failure(
            error,
            document_iri=document,
            location=resource.location
            or (str(resource.content) if isinstance(resource.content, Path) else ""),
            role=resource.role,
        )
        raise

    return SourcePreparation(
        resource,
        document,
        effective,
        syntax,
        selected,
        operation,
        preserve_literals,
        require_complete_graphs,
        compressed_cap,
    )


def _prepare_native(preparation: SourcePreparation) -> PreparedSource:
    """Prepare a graph or native RDF syntax from an already-resolved source."""
    resource = preparation.resource
    document = preparation.document_iri
    effective = preparation.effective_url
    syntax = preparation.syntax
    operation = preparation.budget
    max_bytes = preparation.limits.max_bytes
    max_triples = preparation.limits.max_triples
    preserve_literals = preparation.preserve_literals
    if syntax == "json-ld":
        raise InputError("JSON-LD preparation requires the document dispatcher")
    dataset = Dataset()
    graph_fidelity: TypingLiteral["complete", "nonempty-only"] = "complete"
    if isinstance(resource.content, Dataset | ConjunctiveGraph):
        _charge_graph(resource.content, operation, max_bytes)
        dataset = copy_dataset(resource.content, max_triples)
        is_dataset = True
        fidelity = "supplied_terms"
    elif isinstance(resource.content, Graph):
        _charge_graph(resource.content, operation, max_bytes)
        owned = copy_graph(resource.content, max_triples=max_triples)
        for triple in owned:
            dataset.default_graph.add(triple)
        bind_namespaces(dataset, owned)
        is_dataset = False
        fidelity = "supplied_terms"
    else:
        payload = preparation.read()
        assert syntax is not None
        parsed = parse_dataset(
            payload,
            syntax=syntax,
            base=effective,
            budget=operation,
            max_triples=max_triples,
            preserve_literals=preserve_literals,
        )
        dataset, graph_fidelity = parsed.dataset, parsed.graph_fidelity
        if preparation.require_complete_graphs and graph_fidelity != "complete":
            raise InputError(
                "TriG metadata parser cannot establish complete graph inventory"
            )
        is_dataset = syntax in {"trig", "nquads"}
        fidelity = "source_terms" if preserve_literals else "normalisation_allowed"
    return PreparedSource._from_owned(
        dataset,
        is_dataset=is_dataset,
        document_iri=document,
        effective_url=effective,
        literal_fidelity=fidelity,
        max_triples=max_triples,
        graph_fidelity=graph_fidelity,
        graph_name=resource.graph_name,
        union=resource.union,
    )


def prepare_native(preparation: SourcePreparation) -> PreparedSource:
    """Retain resource identity and structured parser detail on native failures."""
    try:
        return _prepare_native(preparation)
    except InputError as error:
        resource = preparation.resource
        attach_input_failure(
            error,
            document_iri=preparation.document_iri,
            location=resource.location
            or (str(resource.content) if isinstance(resource.content, Path) else ""),
            role=resource.role,
        )
        raise


def prepare_rdf(
    source: RdfSource | Payload,
    *,
    format: str | None = None,
    base: str | None = None,
    compression: Compression | None = None,
    max_compressed_bytes: int | None = None,
    max_bytes: int | None = None,
    max_triples: int | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    require_complete_graphs: bool = False,
) -> PreparedSource:
    """Prepare owned graphs or native RDF syntaxes without JSON-LD processing."""
    return prepare_native(
        prepare_input(
            source,
            format=format,
            base=base,
            compression=compression,
            max_compressed_bytes=max_compressed_bytes,
            max_bytes=max_bytes,
            max_triples=max_triples,
            limits=limits,
            budget=budget,
            preserve_literals=preserve_literals,
            require_complete_graphs=require_complete_graphs,
        )
    )
