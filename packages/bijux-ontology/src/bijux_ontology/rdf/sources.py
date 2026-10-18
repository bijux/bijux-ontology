"""Explicit RDF content descriptors with independent logical resource identities."""

from dataclasses import KW_ONLY, dataclass
from pathlib import Path
from typing import IO, Literal

from rdflib import BNode, Graph

from bijux_ontology.errors import InputError

from .identifiers import absolute_iri

Compression = Literal["gzip"]

Payload = Path | str | bytes | Graph | IO[str] | IO[bytes]


@dataclass(frozen=True)
class RdfSource:
    """Supplied RDF content, its syntax and logical document identity.

    A string is RDF content, never a filename or URL. Use ``Path`` for files.
    Caller streams remain open. ``document_iri`` identifies the document, while
    ``effective_url`` supplies the retrieval base after an intentional alias.
    Dataset resources select an IRI graph, ``@default``, or an explicit union.
    Blank graph names are usable only on their owned prepared snapshot.
    Gzip compression is explicit and requires a declared RDF format, even for
    paths. Only bytes, paths and binary streams can carry compressed content.
    """

    content: Payload
    format: str | None = None
    document_iri: str | None = None
    effective_url: str | None = None
    graph_name: str | None = None
    union: bool = False
    compression: Compression | None = None
    _: KW_ONLY
    location: str = ""
    role: str = "input"

    def __post_init__(self) -> None:
        if (
            not isinstance(self.location, str)
            or not isinstance(self.role, str)
            or not self.role
        ):
            raise InputError("RDF sources require a location string and nonempty role")
        if self.compression is not None:
            if self.compression != "gzip":
                raise InputError("Unsupported RDF compression; choose gzip explicitly")
            if isinstance(self.content, str | Graph):
                raise InputError(
                    "Compressed RDF requires bytes, a path or binary stream"
                )
        if self.format is not None and (
            not isinstance(self.format, str) or not self.format.strip()
        ):
            raise InputError("RDF format must be a nonempty string")
        if type(self.union) is not bool:
            raise InputError("union must be a boolean")
        if self.graph_name is not None:
            if isinstance(self.graph_name, BNode):
                raise InputError(
                    "Blank graph names require selection on an owned snapshot"
                )
            if self.union:
                raise InputError("Choose a graph name or union, not both")
            if self.graph_name != "@default":
                absolute_iri(self.graph_name, location="graph_name")
        for name in ("document_iri", "effective_url"):
            value = getattr(self, name)
            if value is not None:
                absolute_iri(value, location=name)
        if not isinstance(self.content, Path | str | bytes | Graph) and not callable(
            getattr(self.content, "read", None)
        ):
            raise InputError(
                "RDF content must be a path, text/bytes, graph or readable stream"
            )
