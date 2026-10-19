"""Reusable typed Core shapes for a finite selected DCAT 3 application profile."""

from rdflib import BNode, Namespace, URIRef
from rdflib.namespace import DCTERMS, FOAF, RDF, XSD
from rdflib.paths import Path

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.shacl.authoring.compiling import compile_shapes
from bijux_ontology.shacl.authoring.types import (
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)

from .types import (
    DCAT_PROFILE,
    ROLES,
    CatalogRole,
    Dcat3Profile,
    PreparedCatalogProfile,
)

_DEFAULT_PROFILE = Dcat3Profile()

DCAT = Namespace("http://www.w3.org/ns/dcat#")
SHAPES = Namespace("urn:bijux:ontology:dcat3:")
CLASSES = {
    "catalog": DCAT.Catalog,
    "dataset": DCAT.Dataset,
    "series": DCAT.DatasetSeries,
    "distribution": DCAT.Distribution,
    "service": DCAT.DataService,
    "record": DCAT.CatalogRecord,
    "period": DCTERMS.PeriodOfTime,
}


def shape_iri(role: CatalogRole) -> URIRef:
    """Return the stable named root for one selected catalog record role."""
    return SHAPES[role]


def prepare_catalog_profile(
    policy: Dcat3Profile = _DEFAULT_PROFILE,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> PreparedCatalogProfile:
    """Compile reusable ordinary shapes; retain unknown controlled/extension scope."""
    if type(policy) is not Dcat3Profile:
        raise InputError("Catalog preparation requires Dcat3Profile")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    children: list[Report] = []
    definitions: list[ShaclNodeShape | ShaclPropertyShape] = []

    def retain(value: ShaclNodeShape | ShaclPropertyShape) -> ShaclShapeRef:
        """Reserve each authored definition before keeping its shared reference."""
        current.consume("work")
        current.consume("states")
        current.consume("bytes", 192)
        definitions.append(value)
        return ShaclShapeRef(value.identifier)

    def prop(path: URIRef | Path, *constraints: ShaclConstraint) -> ShaclShapeRef:
        """Build one finite property rule under the shared profile inventory."""
        return retain(ShaclPropertyShape(BNode(), path, tuple(constraints)))

    for nodes in (policy.external_services, policy.required_extensions):
        for node in nodes:
            bound_node(node, current)
    for enumeration in (policy.themes, policy.licenses):
        if enumeration is not None:
            for members in (enumeration.values, enumeration.excluded):
                for member in members:
                    bound_node(member, current)
            qualification = enumeration.qualification_report()
            children.append(qualification)
            retain_report(qualification, current, issues)
    for extension in policy.required_extensions:
        issues.append(
            Issue(
                "catalog.extension_unverified",
                "Required extension semantics are outside this DCAT Core profile",
                str(extension),
                kind="unverified",
            )
        )
    for role in ROLES:
        retain(
            ShaclNodeShape(
                SHAPES[role + "Type"],
                (prop(RDF.type, ShaclValue(has_value=CLASSES[role])),),
            )
        )
    dataset_types = retain(
        ShaclNodeShape(
            BNode(),
            constraints=(
                ShaclLogical(
                    "or",
                    (
                        ShaclShapeRef(SHAPES.datasetType),
                        ShaclShapeRef(SHAPES.seriesType),
                    ),
                ),
            ),
        )
    )
    service_type = ShaclShapeRef(SHAPES.serviceType)
    if policy.external_services:
        external = retain(
            ShaclNodeShape(
                BNode(), constraints=(ShaclValue(in_values=policy.external_services),)
            )
        )
        service_type = retain(
            ShaclNodeShape(
                BNode(), constraints=(ShaclLogical("or", (service_type, external)),)
            )
        )
    dates = tuple(
        retain(ShaclNodeShape(BNode(), constraints=(ShaclValue(datatype=str(dt)),)))
        for dt in (XSD.date, XSD.dateTime)
    )
    for role in ROLES:
        properties = [prop(RDF.type, ShaclValue(has_value=CLASSES[role]))]
        if role == "catalog":
            properties += [
                prop(DCAT.dataset, ShaclCount(policy.catalog_datasets), dataset_types),
                prop(DCAT.record, ShaclShapeRef(SHAPES.recordType)),
            ]
        elif role == "dataset":
            properties += [
                prop(
                    DCAT.distribution,
                    ShaclCount(policy.dataset_distributions),
                    ShaclShapeRef(SHAPES.distributionType),
                ),
                prop(DCAT.inSeries, ShaclShapeRef(SHAPES.seriesType)),
            ]
        elif role == "series":
            properties += [
                prop(
                    ~DCAT.inSeries,
                    ShaclCount(policy.series_members),
                    ShaclShapeRef(SHAPES.datasetType),
                )
            ]
            properties += [
                prop(predicate, ShaclShapeRef(SHAPES.datasetType))
                for predicate in (DCAT.first, DCAT.last)
            ]
        elif role == "distribution":
            properties += [
                prop(
                    DCAT.accessURL | DCAT.downloadURL | DCAT.accessService,
                    ShaclCount(policy.distribution_access),
                ),
                prop(DCAT.accessURL, ShaclValue(node_kind="iri")),
                prop(DCAT.downloadURL, ShaclValue(node_kind="iri")),
                prop(DCAT.accessService, service_type),
            ]
        elif role == "service":
            properties += [
                prop(
                    DCAT.endpointURL,
                    ShaclCount(policy.service_endpoints),
                    ShaclValue(node_kind="iri"),
                ),
                prop(DCAT.servesDataset, dataset_types),
            ]
        elif role == "record":
            properties += [
                prop(
                    FOAF.primaryTopic,
                    ShaclCount(policy.record_topics, 1),
                    dataset_types,
                )
            ]
        elif role == "period":
            properties += [
                prop(predicate, ShaclLogical("or", dates))
                for predicate in (DCAT.startDate, DCAT.endDate)
            ]
        if role in ("catalog", "dataset", "series", "distribution", "service"):
            properties += [
                prop(
                    predicate,
                    dataset_types
                    if role in ("dataset", "series")
                    else ShaclShapeRef(SHAPES[role + "Type"]),
                )
                for predicate in (
                    DCAT.previousVersion,
                    DCAT.hasVersion,
                    DCAT.hasCurrentVersion,
                )
            ]
            properties += [prop(DCTERMS.temporal, ShaclShapeRef(SHAPES.periodType))]
            for predicate, enumeration in (
                (DCAT.theme, policy.themes),
                (DCTERMS.license, policy.licenses),
            ):
                rule = ShaclValue(
                    node_kind="iri",
                    in_values=enumeration.values
                    if enumeration is not None and enumeration.qualified
                    else None,
                )
                properties.append(prop(predicate, rule))
        retain(ShaclNodeShape(shape_iri(role), tuple(properties)))
    shapes = compile_shapes(tuple(definitions), budget=current)
    return PreparedCatalogProfile._owned(
        policy,
        shapes,
        Report(
            "catalog.profile",
            1,
            tuple(issues),
            requested=1,
            complete=all(c.complete for c in children)
            and not any(issue.kind != "finding" for issue in issues),
            children=tuple(children),
            regime=DCAT_PROFILE,
        ),
    )
