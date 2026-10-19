"""Explicit negotiated representation contracts and retained finite observations."""

from dataclasses import dataclass
from typing import Literal

from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report
from bijux_ontology.workflows.projects.acquisition.types import (
    AcquiredResource,
    _http_url,
)

_MEDIA = frozenset(
    (
        "text/turtle",
        "application/n-triples",
        "application/rdf+xml",
        "application/trig",
        "application/n-quads",
        "application/ld+json",
        "application/json",
    )
)


def _text(value: str, budget: Budget) -> None:
    """Bound plain contract metadata before validation, hashing or formatting."""
    if type(value) is not str or not value:
        raise InputError("Representation contract metadata requires nonempty text")
    bound_text(value, budget)
    if not value.strip():
        raise InputError("Representation contract metadata requires nonempty text")


@dataclass(frozen=True)
class RepresentationVariant:
    """One exact Accept request and its independently required response binding."""

    name: str
    accept: str
    media_type: str
    profile: str | None = None
    effective_url: str | None = None

    def __post_init__(self) -> None:
        """Qualify explicit media, profile and retrieval identity without IO."""
        budget = Budget()
        qualify_variant(self, budget)


def qualify_variant(value: RepresentationVariant, budget: Budget) -> None:
    """Requalify immutable caller intent under the actual operation allowance."""
    if not isinstance(value, RepresentationVariant):
        raise InputError("Representation variants require typed records")
    for text in (value.name, value.accept, value.media_type):
        _text(text, budget)
    if not value.accept.isascii() or any(
        ord(char) < 32 or ord(char) == 127 for char in value.accept
    ):
        raise InputError("Accept requires printable ASCII header text")
    if value.media_type not in _MEDIA:
        raise InputError("Unsupported explicit RDF representation media type")
    if value.profile is not None:
        _text(value.profile, budget)
        absolute_iri(value.profile)
    if value.effective_url is not None:
        _http_url(value.effective_url, budget)


@dataclass(frozen=True)
class PublishedResource:
    """Explicit logical publication identity and complete or graph-selected scope."""

    iri: str
    url: str
    variants: tuple[RepresentationVariant, ...]
    graph_name: str | None = None

    def __post_init__(self) -> None:
        """Own a finite required variant inventory without guessing publication IRIs."""
        budget = Budget()
        _text(self.iri, budget)
        absolute_iri(self.iri)
        _http_url(self.url, budget)
        if self.graph_name is not None:
            _text(self.graph_name, budget)
            if self.graph_name != "@default":
                absolute_iri(self.graph_name)
        values = capture_items(self.variants, RepresentationVariant, budget)
        if len(values) < 2:
            raise InputError("Agreement requires at least two declared variants")
        names = set()
        for value in values:
            qualify_variant(value, budget)
            budget.consume("states")
            if value.name in names:
                raise InputError("Representation names must be distinct")
            names.add(value.name)
        object.__setattr__(self, "variants", values)


@dataclass(frozen=True)
class VariantAgreement:
    """A required variant's actual response, dependencies and raw selected scopes.

    A missing report is explicitly pending work. Its presence retains the native
    preparation, required resource closure and comparison reports as children.
    """

    variant: RepresentationVariant
    resource: AcquiredResource | None = None
    content_type: str | None = None
    resources: tuple[AcquiredResource, ...] = ()
    requests: int = 0
    report: Report | None = None


@dataclass(frozen=True)
class RepresentationAgreement:
    """Selected RDF agreement; a projection never asserts complete publication truth."""

    publication_iri: str | None
    request_url: str | None
    scope: Literal["complete", "graph-projection"]
    graph_name: str | None
    variants: tuple[VariantAgreement, ...]
    report: Report
