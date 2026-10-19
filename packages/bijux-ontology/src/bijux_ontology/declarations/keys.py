"""Explicit application keys, independent of OWL equality entailment."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal as Choice
from typing import cast

from rdflib import URIRef
from rdflib.paths import Path

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.paths import RdfPath, capture_path


@dataclass(frozen=True)
class KeyComponent:
    """One owned RDF path with an explicit equality and business text policy.

    Normalization applies to string lexical values only, retaining their RDF
    datatype/language dimensions. It cannot qualify semantic value equality.
    """

    path: URIRef | Path | RdfPath
    equality: Choice["rdf-term", "sparql-1.1", "owl2-datatype-map"] = field(
        default="rdf-term", kw_only=True
    )
    normalization: Choice["none", "nfc", "casefold", "nfc-casefold"] = field(
        default="none", kw_only=True
    )

    def __post_init__(self) -> None:
        """Own maintained typed paths and reject ambiguous policy combinations."""
        if self.equality not in ("rdf-term", "sparql-1.1", "owl2-datatype-map"):
            raise InputError("Select an explicit supported key equality regime")
        if self.normalization not in ("none", "nfc", "casefold", "nfc-casefold"):
            raise InputError("Select an explicit supported key normalization")
        if self.equality != "rdf-term" and self.normalization != "none":
            raise InputError("Business normalization requires RDF-term equality")
        object.__setattr__(self, "path", capture_path(self.path))


@dataclass(frozen=True)
class KeyContract:
    """A finite tuple whose scope is supplied records, never owl:hasKey."""

    name: str
    components: tuple[KeyComponent, ...]
    absence: Choice["reject", "exclude", "unverified"] = field(
        default="reject", kw_only=True
    )
    multiplicity: Choice["reject", "unverified"] = field(default="reject", kw_only=True)

    def __post_init__(self) -> None:
        """Capture typed finite components without accepting lazy programs."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise InputError("Key contracts require a nonempty name")
        if not isinstance(self.components, Sequence) or isinstance(
            cast(object, self.components), str | bytes
        ):
            raise InputError("Key components require a finite typed sequence")
        count = len(self.components)
        if not count or count > Limits().max_states:
            raise InputError("Key components require a bounded nonempty inventory")
        values = tuple(self.components[index] for index in range(count))
        if any(not isinstance(value, KeyComponent) for value in values):
            raise InputError("Expected typed KeyComponent declarations")
        if self.absence not in ("reject", "exclude", "unverified"):
            raise InputError("Select an explicit key absence policy")
        if self.multiplicity not in ("reject", "unverified"):
            raise InputError("Select an explicit key multiplicity policy")
        object.__setattr__(self, "components", values)
