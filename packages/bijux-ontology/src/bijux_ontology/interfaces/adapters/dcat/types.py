"""Explicit DCAT 3 record selection and application-profile obligations."""

from dataclasses import dataclass
from typing import Literal

from rdflib import BNode, URIRef

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.sources.snapshots import PreparedSource

CatalogRole = Literal[
    "catalog", "dataset", "series", "distribution", "service", "record", "period"
]
ROLES: tuple[CatalogRole, ...] = (
    "catalog",
    "dataset",
    "series",
    "distribution",
    "service",
    "record",
    "period",
)
DCAT_PROFILE = "dcat-3-selected-catalog"


@dataclass(frozen=True)
class CatalogBinding:
    """One actual resource selected for a specific catalog-profile record role."""

    subject: URIRef | BNode
    role: CatalogRole

    def __post_init__(self) -> None:
        """Require RDF resource identity and a supported explicit record role."""
        if not isinstance(self.subject, URIRef | BNode) or self.role not in ROLES:
            raise InputError("Catalog bindings require a resource and supported role")


@dataclass(frozen=True)
class Dcat3Profile:
    """Caller-selected obligations, not universal W3C vocabulary requirements.

    Defaults require one dataset per selected catalog, distribution per selected
    dataset, member per series, access route per distribution, endpoint per
    service and primary topic per record. Zero disables a minimum. A distribution
    may use an access URL, download URL or service; downloadURL is never mandatory.
    Themes/licenses are checked against supplied finite qualified vocabularies.
    """

    catalog_datasets: int = 1
    dataset_distributions: int = 1
    series_members: int = 1
    distribution_access: int = 1
    service_endpoints: int = 1
    record_topics: int = 1
    themes: ValueEnumeration | None = None
    licenses: ValueEnumeration | None = None
    external_services: tuple[URIRef, ...] = ()
    required_extensions: tuple[URIRef, ...] = ()

    def __post_init__(self) -> None:
        """Keep policy inventories finite and application minima explicit."""
        for value in (
            self.catalog_datasets,
            self.dataset_distributions,
            self.series_members,
            self.distribution_access,
            self.service_endpoints,
            self.record_topics,
        ):
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise InputError("Catalog minima require finite nonnegative integers")
        for enumeration in (self.themes, self.licenses):
            if enumeration is not None and not isinstance(
                enumeration, ValueEnumeration
            ):
                raise InputError("Catalog controlled values require ValueEnumeration")
        for values in (self.external_services, self.required_extensions):
            if type(values) is not tuple or any(
                not isinstance(v, URIRef) for v in values
            ):
                raise InputError("Catalog external references require named IRI tuples")


@dataclass(frozen=True, init=False)
class PreparedCatalogProfile:
    """Factory-owned reusable shapes and their required policy qualification."""

    policy: Dcat3Profile
    shapes: PreparedSource
    report: Report

    def __init__(self) -> None:
        raise InputError("Use prepare_catalog_profile to qualify reusable shapes")

    @classmethod
    def _owned(
        cls, policy: Dcat3Profile, shapes: PreparedSource, report: Report
    ) -> "PreparedCatalogProfile":
        result = object.__new__(cls)
        object.__setattr__(result, "policy", policy)
        object.__setattr__(result, "shapes", shapes)
        object.__setattr__(result, "report", report)
        return result
