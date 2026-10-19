"""Explicit OLS query scope, immutable response receipts and local projections."""

from dataclasses import dataclass
from typing import Literal, cast

from rdflib import URIRef
from rdflib.namespace import RDFS

from bijux_ontology.errors import InputError
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView

CONTROLLER_REVISION = "493545801ca8f5c86f7d15650eed1f2a4cd765ef"

Role = Literal[
    "class",
    "property",
    "objectProperty",
    "dataProperty",
    "annotationProperty",
    "individual",
]


@dataclass(frozen=True)
class OlsQuery:
    """A finite V2 entity query; selected roles are alternatives, never inference.

    Annotation predicates explicitly select lexical service projections. OLS may
    omit original literal language/datatype; this projection does not reconstruct
    them. Imported status and defining-ontology status are independent policies.
    """

    ontology: str
    roles: tuple[Role, ...]
    iri: URIRef | None = None
    hierarchical_ancestor: URIRef | None = None
    annotation_predicates: tuple[URIRef, ...] = (RDFS.label,)
    include_imported: bool = True
    include_obsolete: bool = False
    defining_only: bool = False
    language: str = "en"
    page_size: int = 20

    def __post_init__(self) -> None:
        """Require finite native identity choices without interpreting identifiers."""
        if (
            type(self.ontology) is not str
            or not self.ontology
            or type(self.roles) is not tuple
            or not self.roles
            or any(
                type(cast(object, role)) is not str
                or role
                not in (
                    "class",
                    "property",
                    "objectProperty",
                    "dataProperty",
                    "annotationProperty",
                    "individual",
                )
                for role in self.roles
            )
            or any(
                type(value) is not bool
                for value in (
                    self.include_imported,
                    self.include_obsolete,
                    self.defining_only,
                )
            )
            or type(self.language) is not str
            or not self.language
            or type(self.page_size) is not int
            or not 1 <= self.page_size <= 1000
            or type(self.annotation_predicates) is not tuple
            or any(type(p) is not URIRef for p in self.annotation_predicates)
            or self.iri is not None
            and type(self.iri) is not URIRef
            or self.hierarchical_ancestor is not None
            and type(self.hierarchical_ancestor) is not URIRef
        ):
            raise InputError(
                "OLS queries need explicit ontology, roles and finite policies"
            )


@dataclass(frozen=True)
class OlsPage:
    """Captured exact response and fresh ontology metadata bracketing that page.

    These are supplied/acquired observations, not a transactional server snapshot
    or proof that a remote service's data-version declaration is immutable.
    """

    url: str
    content: bytes
    ontology_before: bytes
    ontology_after: bytes
    software_release: str | None = None

    def __post_init__(self) -> None:
        """Accept immutable source bytes and separate optional software identity."""
        if (
            type(self.url) is not str
            or any(
                type(v) is not bytes
                for v in (self.content, self.ontology_before, self.ontology_after)
            )
            or self.software_release is not None
            and type(self.software_release) is not str
        ):
            raise InputError(
                "OLS page receipts require immutable bytes and a request URL"
            )


@dataclass(frozen=True)
class OlsAnnotation:
    """One selected lexical value with its unaltered qualified JSON attachment."""

    predicate: URIRef
    lexical: str
    qualified_json: bytes


@dataclass(frozen=True)
class OlsEntity:
    """An ontology-local entity; generic ancestors are service relations only."""

    ontology: str
    iri: URIRef
    roles: tuple[str, ...]
    imported: bool
    defining: bool
    obsolete: bool
    annotations: tuple[OlsAnnotation, ...]
    hierarchical_ancestors: tuple[URIRef, ...]
    raw_json: bytes


@dataclass(frozen=True, init=False)
class OlsSnapshot:
    """Owned selected service evidence, independent of future network requests.

    The vocabulary contains only role declarations and selected lexical service
    annotations. It is not the original ontology, a global closed catalogue, or
    an OWL entailment result. Completeness belongs only to this exact query.
    """

    query: OlsQuery | None
    base_url: str
    api_version: str
    controller_revision: str
    data_version: str | None
    loaded: str | None
    pages: tuple[OlsPage, ...]
    entities: tuple[OlsEntity, ...]
    vocabulary: VocabularyView | None
    complete: bool
    report: Report

    def __init__(self, *args: object, **kwargs: object) -> None:
        """Require checked response preparation instead of caller-certified truth."""
        raise InputError("Use prepare_ols_snapshot or acquire_ols_snapshot")

    @classmethod
    def _owned(
        cls,
        query: OlsQuery | None,
        base_url: str,
        data_version: str | None,
        loaded: str | None,
        pages: tuple[OlsPage, ...],
        entities: tuple[OlsEntity, ...],
        vocabulary: VocabularyView | None,
        complete: bool,
        report: Report,
    ) -> "OlsSnapshot":
        """Adopt already paid immutable records without repeating allocation work."""
        result = object.__new__(cls)
        for key, value in (
            ("query", query),
            ("base_url", base_url),
            ("api_version", "v2"),
            ("controller_revision", CONTROLLER_REVISION),
            ("data_version", data_version),
            ("loaded", loaded),
            ("pages", pages),
            ("entities", entities),
            ("vocabulary", vocabulary),
            ("complete", complete),
            ("report", report),
        ):
            object.__setattr__(result, key, value)
        return result


@dataclass(frozen=True)
class OlsLookup:
    """Presence/absence is scoped to an exact ontology, role and supplied query."""

    entities: tuple[OlsEntity, ...]
    status: Literal["present", "absent", "unknown"]
    report: Report
