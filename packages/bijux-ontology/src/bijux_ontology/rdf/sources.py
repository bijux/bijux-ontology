"""Explicit RDF content descriptors with independent logical resource identities."""

from dataclasses import dataclass
from pathlib import Path
from typing import IO

from rdflib import Graph

from bijux_ontology.errors import InputError

from .identifiers import absolute_iri

Payload = Path | str | bytes | Graph | IO[str] | IO[bytes]


@dataclass(frozen=True)
class RdfSource:
    """Supplied RDF content, its syntax and logical document identity.

    A string is RDF content, never a filename or URL. Use ``Path`` for files.
    Caller streams remain open. ``document_iri`` identifies the document, while
    ``effective_url`` supplies the retrieval base after an intentional alias.
    """

    content: Payload
    format: str | None = None
    document_iri: str | None = None
    effective_url: str | None = None

    def __post_init__(self) -> None:
        if self.format is not None and (
            not isinstance(self.format, str) or not self.format.strip()
        ):
            raise InputError("RDF format must be a nonempty string")
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
