"""Explicit versioned RDF triple-term ownership, disjoint from RDF 1.1 sources."""

from dataclasses import dataclass

import pyoxigraph as ox

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report

TRIPLE_TERM_CONTRACT = "rdf12-cr-20260407-pyoxigraph-0.5.11"
TRIPLE_TERM_SPECIFICATION = "https://www.w3.org/TR/2026/CR-rdf12-concepts-20260407/"


@dataclass(frozen=True, init=False)
class TripleTermSnapshot:
    """Factory-owned immutable parser terms, including exact nested literals.

    This graph-only profile is not a PreparedSource or an RDFLib graph. Quads
    have only the default graph. Described triples are terms, never additional
    assertions. Backend Store insertion is deliberately excluded because it can
    normalize lexical forms. Blank identity belongs to this one parser capture;
    matching labels in another snapshot do not establish correspondence.
    """

    quads: tuple[ox.Quad, ...]
    source_sha256: str
    document_iri: str | None
    effective_url: str | None
    contract: str

    def __init__(self) -> None:
        raise InputError("Triple-term snapshots require prepare_triple_terms")

    @classmethod
    def _from_owned(
        cls,
        quads: tuple[ox.Quad, ...],
        source_sha256: str,
        document_iri: str | None,
        effective_url: str | None,
    ) -> "TripleTermSnapshot":
        """Adopt only operation-qualified actual immutable backend terms."""
        value = object.__new__(cls)
        for name, item in (
            ("quads", quads),
            ("source_sha256", source_sha256),
            ("document_iri", document_iri),
            ("effective_url", effective_url),
            ("contract", TRIPLE_TERM_CONTRACT),
        ):
            object.__setattr__(value, name, item)
        return value


@dataclass(frozen=True)
class TripleTermPreparation:
    """Complete owned graph or an explicit incomplete preparation report."""

    snapshot: TripleTermSnapshot | None
    report: Report


@dataclass(frozen=True)
class TripleTermInspection:
    """Actual nested term occurrences, retaining their enclosing assertions."""

    snapshot: TripleTermSnapshot
    triple_terms: tuple[ox.Triple, ...]
    report: Report


@dataclass(frozen=True)
class TripleTermExport:
    """Qualified N-Triples 1.2 bytes; no RDF 1.1 serialization is implied."""

    content: bytes | None
    report: Report
    format: str = "nt-1.2"
