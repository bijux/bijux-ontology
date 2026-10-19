"""The descriptive PROF subset; RDF descriptors never instantiate executable jobs."""

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import DCTERMS, RDF, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype

from .loading import _qualify_profile, _text
from .types import DescribedArtifact, ProfileDescription, ValidationProfile

PROF = Namespace("http://www.w3.org/ns/dx/prof/")


def profile_to_prof(
    profile: ValidationProfile,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ProfileDescription:
    """Describe required parents and explicit artifacts, not executable conformance.

    Optional layer selection has no equivalent in this subset and remains unknown.
    Artifact paths are intentionally not converted into remotely fetched URLs.
    """
    if not isinstance(profile, ValidationProfile):
        raise InputError("Expected an owned profile manifest")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Profile description limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    source = None
    parents: list[str] = []
    artifacts: list[DescribedArtifact] = []
    qualified_iri = ""
    try:
        profile = _qualify_profile(profile, operation)
        qualified_iri = profile.iri
        root = URIRef(qualified_iri)
        graph = Graph()

        def add(subject: Node, predicate: URIRef, value: Node) -> None:
            """Bound generated ordinary descriptive RDF before ownership transfer."""
            operation.consume("work")
            operation.consume("triples")
            operation.consume("states")
            for term in (subject, predicate, value):
                bound_node(term, operation)
            graph.add((subject, predicate, value))

        add(root, RDF.type, PROF.Profile)
        for parent in profile.parents:
            operation.consume("work")
            if parent.required:
                iri = _text(parent.iri, operation, iri=True)
                parents.append(iri)
                add(root, PROF.isProfileOf, URIRef(iri))
            else:
                issues.append(
                    Issue(
                        "profile.description_unverified",
                        "Optional parent selection is outside "
                        "the descriptive PROF subset",
                        kind="unverified",
                    )
                )
        for artifact in profile.artifacts:
            operation.consume("work")
            iri = _text(artifact.iri, operation, iri=True)
            node = BNode()
            add(root, PROF.hasResource, node)
            add(node, RDF.type, PROF.ResourceDescriptor)
            add(node, PROF.hasArtifact, URIRef(iri))
            roles = []
            for role in artifact.roles:
                role = _text(role, operation, iri=True)
                roles.append(role)
                add(node, PROF.hasRole, URIRef(role))
            if artifact.format is not None:
                add(node, DCTERMS.format, Literal(_text(artifact.format, operation)))
            artifacts.append(DescribedArtifact(iri, tuple(roles), artifact.format))
        source = prepare_rdf(graph, budget=operation)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("profile.description_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "profile-description",
        1 if source is not None else 0,
        tuple(issues),
        requested=1,
        regime="descriptive-prof-subset",
    )
    return ProfileDescription(
        qualified_iri, tuple(parents), tuple(artifacts), source, report
    )


def read_prof(
    source: PreparedSource,
    *,
    profile_iri: str,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ProfileDescription:
    """Read one explicit profile description without fetching or creating checks.

    Direct isProfileOf links, hasResource/hasArtifact/hasRole and literal dct:format
    are supported. No optionality, executable manifests or inferred transitive
    conformance are manufactured from RDF metadata.
    """
    if not isinstance(source, PreparedSource):
        raise InputError(
            "PROF reading requires an owned explicitly selected RDF source"
        )
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("PROF description limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    parents: list[str] = []
    artifacts: list[DescribedArtifact] = []
    iri = ""
    checked = 0
    try:
        iri = _text(profile_iri, operation, iri=True)
        root = URIRef(iri)
        graph = source._read(budget=operation)

        def values(node: Node, predicate: URIRef) -> tuple[Node, ...]:
            """Capture only this requested relation under finite metadata bounds."""
            result = []
            for value in graph.objects(node, predicate):
                operation.consume("work")
                operation.consume("states")
                bound_node(value, operation)
                result.append(value)
            return tuple(result)

        if (root, RDF.type, PROF.Profile) not in graph:
            raise InputError(
                "Selected resource is not an explicitly declared PROF profile"
            )
        for parent in values(root, PROF.isProfileOf):
            if not isinstance(parent, URIRef):
                raise InputError("PROF parents require named IRIs")
            parents.append(_text(parent, operation, iri=True))
        for descriptor in values(root, PROF.hasResource):
            if not isinstance(descriptor, URIRef | BNode):
                raise InputError("PROF descriptors require RDF resources")
            names = values(descriptor, PROF.hasArtifact)
            if len(names) != 1 or not isinstance(names[0], URIRef):
                raise InputError("A supported PROF descriptor needs one named artifact")
            name = _text(names[0], operation, iri=True)
            roles = []
            for role in values(descriptor, PROF.hasRole):
                if not isinstance(role, URIRef):
                    raise InputError("PROF roles require named IRIs")
                roles.append(_text(role, operation, iri=True))
            formats = values(descriptor, DCTERMS.format)
            syntax = None
            if formats:
                if (
                    len(formats) != 1
                    or not isinstance(formats[0], Literal)
                    or literal_datatype(formats[0]) != XSD.string
                ):
                    raise InputError(
                        "Supported PROF format is one untagged string literal"
                    )
                syntax = _text(str(formats[0]), operation)
            artifacts.append(DescribedArtifact(name, tuple(sorted(roles)), syntax))
        checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue("profile.description_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "profile-description",
        checked,
        tuple(issues),
        requested=1,
        regime="descriptive-prof-subset",
    )
    return ProfileDescription(
        iri,
        tuple(sorted(parents)),
        tuple(sorted(artifacts, key=lambda item: item.iri)),
        source,
        report,
    )
