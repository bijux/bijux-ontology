"""Bounded native RDF loading and operation-owned source preparation."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal as TypingLiteral

from rdflib import ConjunctiveGraph, Dataset, Graph, Literal, URIRef

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits

from .identifiers import absolute_iri, language_tag
from .parsing import RDF_FORMATS, parse_dataset
from .snapshots import PreparedSource, bind_namespaces, copy_dataset, copy_graph
from .sources import Payload, RdfSource

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
    if supplied is None and isinstance(source.content, Path):
        supplied = source.content.suffix.removeprefix(".")
    if not supplied:
        raise InputError("RDF bytes, text and streams require an explicit format")
    supplied = _ALIASES.get(supplied.lower(), supplied.lower())
    if supplied not in RDF_FORMATS and supplied != "json-ld":
        raise InputError(f"Unsupported RDF format: {supplied}")
    return supplied


def _read(content: Payload, max_bytes: int) -> bytes:
    if isinstance(content, Path):
        try:
            with content.open("rb") as stream:
                payload = stream.read(max_bytes + 1)
        except OSError as error:
            raise ResourceError(f"Cannot read RDF source: {content}") from error
    elif isinstance(content, bytes):
        payload = content
    elif isinstance(content, str):
        if len(content) > max_bytes:
            raise LimitExceeded("RDF text exceeds max_bytes")
        try:
            payload = content.encode("utf-8")
        except UnicodeError as error:
            raise InputError("RDF text is not valid UTF-8 content") from error
    elif isinstance(content, Graph):
        raise InputError("Parsed graphs are not byte streams")
    else:
        try:
            data = content.read(max_bytes + 1)
        except (OSError, ValueError) as error:
            raise ResourceError("Cannot read RDF source stream") from error
        except TypeError as error:
            raise InputError("RDF stream must support bounded read(size)") from error
        if not isinstance(data, str | bytes):
            raise InputError("RDF stream must return text or bytes")
        try:
            payload = data.encode("utf-8") if isinstance(data, str) else data
        except UnicodeError as error:
            raise InputError("RDF stream text is not valid UTF-8 content") from error
    if len(payload) > max_bytes:
        raise LimitExceeded("RDF resource exceeds max_bytes")
    return payload


def _charge_graph(source: Graph, budget: Budget, max_bytes: int) -> None:
    """Bound supplied graph terms before owned copying or index construction."""
    records = (
        source.quads() if isinstance(source, Dataset | ConjunctiveGraph) else source
    )
    size = 0
    for record in records:
        budget.consume("triples")
        for term in record:
            if isinstance(term, Graph):
                term = term.identifier
            text = str(term)
            if len(text) > max_bytes:
                raise LimitExceeded("RDF term exceeds max_bytes")
            amount = len(text.encode("utf-8"))
            size += amount
            if size > max_bytes:
                raise LimitExceeded("RDF graph term text exceeds max_bytes")
            budget.consume("bytes", amount)
            if isinstance(term, URIRef):
                absolute_iri(text)
            if isinstance(term, Literal) and term.datatype:
                absolute_iri(str(term.datatype))
            if isinstance(term, Literal) and term.language:
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

    def read(self) -> bytes:
        """Read bounded source bytes and charge them to the shared budget."""
        payload = _read(self.resource.content, self.limits.max_bytes)
        self.budget.consume("bytes", len(payload))
        return payload


def prepare_input(
    source: RdfSource | Payload,
    *,
    format: str | None = None,
    base: str | None = None,
    max_bytes: int | None = None,
    max_triples: int | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve_literals: bool = True,
    require_complete_graphs: bool = False,
) -> SourcePreparation:
    """Resolve one source and budget without reading or parsing its content."""
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
    operation = budget if budget is not None else Budget(selected)
    max_bytes = selected.max_bytes
    max_triples = selected.max_triples
    operation.consume("resources")
    resource = (
        source if isinstance(source, RdfSource) else RdfSource(source, format, base)
    )
    if isinstance(source, RdfSource):
        if format is not None and resource.format not in (None, format):
            raise InputError("Conflicting RDF source format declarations")
        if base is not None and resource.document_iri not in (None, base):
            raise InputError("Conflicting RDF source identities")
        resource = replace(
            resource,
            format=resource.format or format,
            document_iri=resource.document_iri or base,
        )
    document = resource.document_iri
    if document is None and isinstance(resource.content, Path):
        document = resource.content.resolve().as_uri()
    document = absolute_iri(document) if document is not None else None
    effective = resource.effective_url or document
    if effective is not None:
        absolute_iri(effective)
    syntax = None if isinstance(resource.content, Graph) else _format(resource)
    return SourcePreparation(
        resource,
        document,
        effective,
        syntax,
        selected,
        operation,
        preserve_literals,
        require_complete_graphs,
    )


def prepare_native(preparation: SourcePreparation) -> PreparedSource:
    """Prepare a graph or native RDF syntax from an already-resolved source."""
    resource = preparation.resource
    document = preparation.document_iri
    effective = preparation.effective_url
    syntax = preparation.syntax
    operation = preparation.budget
    max_bytes = preparation.limits.max_bytes
    max_triples = preparation.limits.max_triples
    preserve_literals = preparation.preserve_literals
    require_complete_graphs = preparation.require_complete_graphs
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
        if syntax == "trig":
            graph_fidelity = "nonempty-only"
            if require_complete_graphs:
                raise InputError(
                    "TriG parser cannot guarantee preservation of empty named graphs"
                )
        payload = preparation.read()
        assert syntax is not None
        dataset = parse_dataset(
            payload,
            syntax=syntax,
            base=effective,
            budget=operation,
            max_triples=max_triples,
            preserve_literals=preserve_literals,
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


def prepare_rdf(
    source: RdfSource | Payload,
    *,
    format: str | None = None,
    base: str | None = None,
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
            max_bytes=max_bytes,
            max_triples=max_triples,
            limits=limits,
            budget=budget,
            preserve_literals=preserve_literals,
            require_complete_graphs=require_complete_graphs,
        )
    )
