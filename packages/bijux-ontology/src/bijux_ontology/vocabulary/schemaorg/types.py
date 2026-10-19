"""Exact supplied-release provenance and finite Schema.org expectation evidence."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal as Choice

from rdflib import BNode, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node

Triple = tuple[Node, Node, Node]
Mode = Choice["published", "application"]


def _iri(value: object, budget: Budget) -> URIRef:
    """Reject erased RDF kinds and qualify named IRI metadata before copying it."""
    if not isinstance(value, URIRef):
        raise InputError("Schema.org identifiers require exact URIRef terms")
    bound_node(value, budget)
    return value


def _metadata(
    release: str,
    namespace: URIRef,
    extensions: Sequence[URIRef],
    mapping: Sequence[tuple[URIRef, URIRef]],
    budget: Budget,
) -> tuple[tuple[URIRef, ...], tuple[tuple[URIRef, URIRef], ...]]:
    """Capture finite exact choices once, rejecting ambiguous or chained mappings."""
    if not isinstance(release, str) or isinstance(release, BNode | Literal):
        raise InputError("Schema.org releases require a caller-supplied text label")
    bound_text(release, budget)
    if not release:
        raise InputError("Schema.org release label cannot be empty")
    _iri(namespace, budget)
    if namespace not in (URIRef("http://schema.org/"), URIRef("https://schema.org/")):
        raise InputError("Select the exact http or https Schema.org namespace")
    selected = capture_items(extensions, URIRef, budget)
    seen_extensions: set[URIRef] = set()
    for extension in selected:
        _iri(extension, budget)
        budget.consume("states")
        if extension in seen_extensions:
            raise InputError("Schema.org extension choices must be distinct")
        seen_extensions.add(extension)
    pairs = capture_items(mapping, object, budget)
    aliases: dict[URIRef, URIRef] = {}
    for pair in pairs:
        if type(pair) is not tuple or len(pair) != 2:
            raise InputError("IRI mappings require immutable source/target pairs")
        key, target = (_iri(pair[0], budget), _iri(pair[1], budget))
        if key in aliases:
            raise InputError("IRI mapping sources must be distinct")
        budget.consume("states", 2)
        aliases[key] = target
    for key, target in aliases.items():
        budget.consume("work")
        if target in aliases and key != target:
            raise InputError("IRI mappings must name final targets, without chains")
    return selected, tuple(aliases.items())


@dataclass(frozen=True, init=False)
class SchemaOrgView:
    """One owned release selection, never a global live Schema.org registry.

    The label is supplied by the caller; sha256 records the exact acquired bytes,
    not an authenticity assertion. Namespace, extension and explicit alias choices
    remain separate from original RDF identities. Ordinary factory-created views
    own the parsed snapshot and all immutable selection metadata.
    """

    source: PreparedSource | None
    release: str
    namespace: URIRef | None
    sha256: str
    syntax: str | None
    extensions: tuple[URIRef, ...]
    iri_mapping: tuple[tuple[URIRef, URIRef], ...]
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require acquisition through prepare_schemaorg to bind bytes and source."""
        raise InputError("Use prepare_schemaorg to acquire a byte-pinned view")

    @classmethod
    def _from_owned(
        cls,
        source: PreparedSource | None,
        release: str,
        namespace: URIRef | None,
        sha256: str,
        syntax: str | None,
        extensions: tuple[URIRef, ...],
        iri_mapping: tuple[tuple[URIRef, URIRef], ...],
        report: Report,
    ) -> "SchemaOrgView":
        """Adopt factory-qualified immutable metadata without resetting its budget."""
        result = object.__new__(cls)
        for name, value in (
            ("source", source),
            ("release", release),
            ("namespace", namespace),
            ("sha256", sha256),
            ("syntax", syntax),
            ("extensions", extensions),
            ("iri_mapping", iri_mapping),
            ("report", report),
        ):
            object.__setattr__(result, name, value)
        return result


@dataclass(frozen=True)
class SchemaPropertyExpectation:
    """Alternative endpoint hints with original inverse and lifecycle assertions."""

    requested: URIRef | None
    predicate: URIRef | None
    domains: tuple[Node, ...]
    ranges: tuple[Node, ...]
    inverses: tuple[Node, ...]
    superseded_by: tuple[Node, ...]
    assertions: tuple[Triple, ...]
    report: Report


@dataclass(frozen=True)
class SchemaOrgUse:
    """One actual assertion and its finite, separately interpreted guidance."""

    statement: Triple
    expectation: SchemaPropertyExpectation
    domain_matches: bool | None
    range_matches: bool | None
    used_text_fallback: bool


@dataclass(frozen=True)
class SchemaOrgCheck:
    """Raw advisory truth and optional required application-policy truth.

    Published guidance is not an OWL consistency test. In application mode the
    required report is the application report; raw guidance remains available
    separately, so an explicit fallback does not erase the published mismatch.
    """

    view: SchemaOrgView | None
    source: PreparedSource | None
    graph_name: GraphSelection | None
    union: bool
    uses: tuple[SchemaOrgUse, ...]
    published: Report
    application: Report | None
    report: Report
