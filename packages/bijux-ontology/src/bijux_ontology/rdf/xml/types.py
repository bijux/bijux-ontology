"""Qualified XML input metadata and explicit finite capability failures."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError

XMLEncoding = Literal["utf-8", "utf-16-le", "utf-16-be"]


class XMLCapabilityError(InputError):
    """A valid XML construct is outside the native bounded RDF/XML profile."""

    def __init__(self, feature: str) -> None:
        """Retain a trusted capability name without embedding supplied XML."""
        self.feature = feature
        super().__init__("Unsupported RDF/XML capability: " + feature)


@dataclass(frozen=True)
class XMLInput:
    """UTF-8 parser bytes with original source encoding and expanded-size evidence.

    UTF-16 transcoding preserves Unicode characters and line positions. Only a
    validated encoding declaration changes, with equal-width external padding.
    XML markup, namespace bindings, entity declarations and body stay intact.
    """

    payload: bytes
    encoding: XMLEncoding
    internal_entities: int
    expanded_bytes: int
