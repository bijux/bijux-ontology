"""Native asserted XKOS 1.2 groups over explicit supplied release scopes."""

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, SKOS
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.browsing.navigation import scheme_members

from .types import XKOS as X
from .types import (
    AssociationKind,
    AssociationRequirement,
    ClassificationRelease,
    ConceptAssociation,
    Correspondence,
    Resource,
    _ReleaseEvidence,
)


def reserve(budget: Budget, count: int = 1) -> None:
    """Reserve immutable output/reference capacity before retaining observations."""
    budget.consume("states", count)
    budget.consume("bytes", 128 * count)


def physical(
    source: PreparedSource, graph_name: GraphSelection | None, budget: Budget
) -> tuple[Resource | None, Graph, SnapshotMetadata]:
    """Read exactly one authorized graph, including actual anonymous graph authority."""
    metadata = SnapshotMetadata.capture(source, budget)
    if (
        metadata.union
        or metadata.graph_fidelity != "complete"
        or metadata.literal_fidelity not in ("source_terms", "supplied_terms")
    ):
        raise ResourceError("XKOS requires one complete physical graph, never a union")
    if isinstance(graph_name, BNode) and not any(
        name is graph_name for name in source._graph_names(budget=budget)
    ):
        raise InputError("Blank graph selection requires exact snapshot ownership")
    if isinstance(metadata.graph_name, BNode) and not any(
        name is metadata.graph_name for name in source._graph_names(budget=budget)
    ):
        raise InputError("Pinned blank graph requires exact snapshot ownership")
    contexts = source._contexts(graph_name, budget=budget)
    _, selected, graph = next(contexts)
    if next(contexts, None) is not None:
        raise ResourceError("Select exactly one physical XKOS graph")
    return selected, graph, metadata


def owns(graph: Graph, node: BNode, budget: Budget) -> bool:
    """Bound actual identity lookup without granting authority to an equal label."""
    for triple in graph:
        budget.consume("work", 3)
        if any(node is term for term in triple):
            return True
    return False


def values(
    graph: Graph, subject: Node, predicate: URIRef, budget: Budget
) -> tuple[Node, ...]:
    """Capture all original objects, including unsupported terms, without coercion."""
    result = []
    for value in graph.objects(subject, predicate):
        budget.consume("work")
        budget.consume("triples")
        bound_node(value, budget)
        reserve(budget)
        result.append(value)
    return tuple(result)


def issue(
    issues: IssueCollector,
    code: str,
    message: str,
    node: Node,
    *,
    unknown: bool = False,
) -> None:
    """Keep diagnostics bound to actual RDF resources or malformed source terms."""
    bound_node(node, issues.budget)
    issues.append(
        Issue(code, message, str(node), kind="unverified" if unknown else "finding")
    )


def release_evidence(
    release: ClassificationRelease,
    budget: Budget,
    issues: IssueCollector,
    children: list[Report],
) -> _ReleaseEvidence:
    """Reuse one existing owned vocabulary scope and native scheme membership."""
    if (
        type(release) is not ClassificationRelease
        or type(release.scheme) is not URIRef
        or not isinstance(release.vocabulary, VocabularyView)
        or type(release.complete) is not bool
    ):
        raise InputError(
            "Select an exact scheme IRI, VocabularyView and inventory completeness"
        )
    bound_node(release.scheme, budget)
    view = release.vocabulary
    scopes = view._reader.scopes
    if len(scopes) != 1:
        raise ResourceError(
            "A release view must select one supplied physical source scope"
        )
    scope, selected = scopes[0]
    budget.consume("work", len(view._vocabulary._source_handles))
    handle = next(
        (source for role, source in view._vocabulary._source_handles if role == scope),
        None,
    )
    if handle is None or handle.selected is not selected or handle.union:
        raise ResourceError("Release vocabulary scope lacks physical source ownership")
    _, graph, metadata = physical(handle.prepared, handle.graph_name, budget)
    if graph is not selected:
        raise ResourceError("Release vocabulary index is not its exact selected graph")
    information = view.lookup(str(release.scheme))
    budget.consume("work", len(information.kinds) + 1)
    if (
        information.status != "declared"
        or str(SKOS.ConceptScheme) not in information.kinds
    ):
        issue(
            issues,
            "xkos.release_missing",
            "Selected release scheme lacks its own supplied declaration",
            release.scheme,
        )
    membership = scheme_members(view, str(release.scheme), budget=budget)
    retain_report(membership.report, budget, issues)
    children.append(membership.report)
    reserve(budget, len(membership.items) + 1)
    members = set()
    for member in membership.items:
        budget.consume("work")
        bound_node(member.member, budget)
        if isinstance(member.member, URIRef):
            members.add(member.member)
    return _ReleaseEvidence(release, handle.prepared, metadata, frozenset(members))


def endpoint(
    value: Node, release: _ReleaseEvidence, budget: Budget, issues: IssueCollector
) -> bool:
    """Require declaration and membership in this release, not a similarly named one."""
    bound_node(value, budget)
    if not isinstance(value, URIRef):
        issue(
            issues,
            "xkos.endpoint_unverified",
            "Endpoint is not a named category with cross-source release identity",
            value,
            unknown=True,
        )
        return False
    info = release.release.vocabulary.lookup(str(value))
    budget.consume("work", len(info.kinds) + 1)
    if (
        value not in release.members
        or info.status != "declared"
        or str(SKOS.Concept) not in info.kinds
    ):
        issue(
            issues,
            "xkos.endpoint_missing",
            "Category lacks declaration/membership in the selected release",
            value,
            unknown=not release.release.complete,
        )
        return False
    return True


def kind(sources: tuple[Node, ...], targets: tuple[Node, ...]) -> AssociationKind:
    """Classify one intact group by its observed source/target arity."""
    left, right = len(sources), len(targets)
    return (
        "empty"
        if not left and not right
        else "creation"
        if not left
        else "deletion"
        if not right
        else "one-to-one"
        if left == right == 1
        else "split"
        if left == 1
        else "merge"
        if right == 1
        else "many-to-many"
    )


def read_correspondence(
    source: PreparedSource | Graph,
    identifier: Resource,
    *,
    source_release: ClassificationRelease,
    target_release: ClassificationRelease,
    graph_name: GraphSelection | None = None,
    require_sources: bool = True,
    require_targets: bool = True,
    requirements: tuple[AssociationRequirement, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Correspondence:
    """Read complete authored groups in one selected asserted XKOS 1.2 scope.

    Required nonempty sides and optional exact side inventories are consumer
    contracts: XKOS itself permits creation/deletion associations. Each side is
    a union of categories, never a set of pairwise exactMatch assertions. Unknown
    source annotations remain in the full original snapshot; no allocation,
    equivalence, OWL truth or global classification coverage is inferred.
    """
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("XKOS requires Limits")
    current = operation(limits, budget)
    issues = IssueCollector(current)
    children: list[Report] = []
    associations: list[ConceptAssociation] = []
    prepared = None
    selected = None
    metadata = None
    releases: tuple[_ReleaseEvidence, ...] = ()
    types: tuple[Node, ...] = ()
    compares: tuple[Node, ...] = ()
    links: tuple[Node, ...] = ()
    complete = False
    try:
        if (
            not isinstance(identifier, URIRef | BNode)
            or type(require_sources) is not bool
            or type(require_targets) is not bool
        ):
            raise InputError(
                "Select an RDF table resource and explicit side requirements"
            )
        bound_node(identifier, current)
        if not isinstance(source, PreparedSource | Graph):
            raise InputError("XKOS requires an actual supplied RDF source")
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, budget=current)
        )
        selected, graph, metadata = physical(prepared, graph_name, current)
        if isinstance(identifier, BNode) and not owns(graph, identifier, current):
            raise InputError(
                "Anonymous correspondence requires actual owned source identity"
            )
        releases = (
            release_evidence(source_release, current, issues, children),
            release_evidence(target_release, current, issues, children),
        )
        if source_release.scheme == target_release.scheme:
            raise InputError("Select distinct source and target version-scheme IRIs")
        types = values(graph, identifier, RDF.type, current)
        if X.Correspondence not in types:
            issue(
                issues,
                "xkos.correspondence_type",
                "Selected table lacks its Correspondence declaration",
                identifier,
            )
        compares = values(graph, identifier, X.compares, current)
        if set(compares) != {source_release.scheme, target_release.scheme}:
            issue(
                issues,
                "xkos.release_mismatch",
                "Compared scheme IRIs do not match the exact selected releases",
                identifier,
            )
        links = values(graph, identifier, X.madeOf, current)
        if not links:
            issue(
                issues,
                "xkos.associations_missing",
                "Selected correspondence has no supplied association groups",
                identifier,
            )
        for link in links:
            current.consume("work")
            if not isinstance(link, URIRef | BNode):
                issue(
                    issues,
                    "xkos.association_invalid",
                    "madeOf must identify an association resource",
                    link,
                )
                continue
            group_types = values(graph, link, RDF.type, current)
            if X.ConceptAssociation not in group_types:
                issue(
                    issues,
                    "xkos.association_type",
                    "A supplied group lacks its ConceptAssociation declaration",
                    link,
                )
            sources = values(graph, link, X.sourceConcept, current)
            targets = values(graph, link, X.targetConcept, current)
            if (
                not sources
                and not targets
                or require_sources
                and not sources
                or require_targets
                and not targets
            ):
                issue(
                    issues,
                    "xkos.side_missing",
                    "An explicitly required association side is empty",
                    link,
                )
            for value in sources:
                endpoint(value, releases[0], current, issues)
            for value in targets:
                endpoint(value, releases[1], current, issues)
            reserve(current)
            associations.append(
                ConceptAssociation(
                    link, group_types, sources, targets, kind(sources, targets)
                )
            )
        if type(requirements) is not tuple:
            raise InputError("Exact side requirements need an immutable tuple")
        reserve(current, len(requirements))
        seen: set[Resource] = set()
        for requirement in requirements:
            if (
                type(requirement) is not AssociationRequirement
                or not isinstance(requirement.association, URIRef | BNode)
                or type(requirement.sources) is not tuple
                or type(requirement.targets) is not tuple
            ):
                raise InputError("Expected typed exact association side inventories")
            bound_node(requirement.association, current)
            if requirement.association in seen:
                raise InputError("Select each association requirement once")
            seen.add(requirement.association)
            for side in (requirement.sources, requirement.targets):
                reserve(current, len(side))
                for member in side:
                    if type(member) is not URIRef:
                        raise InputError(
                            "Required sides contain exact named category IRIs"
                        )
                    bound_node(member, current)
                if len(set(side)) != len(side):
                    raise InputError("Required group side members must be distinct")
            if isinstance(requirement.association, BNode) and not any(
                group.identifier is requirement.association for group in associations
            ):
                raise InputError(
                    "Anonymous group requirement requires exact owned identity"
                )
            current.consume("work", len(associations))
            observed = next(
                (
                    group
                    for group in associations
                    if group.identifier == requirement.association
                ),
                None,
            )
            if (
                observed is None
                or set(observed.sources) != set(requirement.sources)
                or set(observed.targets) != set(requirement.targets)
            ):
                issue(
                    issues,
                    "xkos.side_inventory",
                    "Group does not retain its complete required source/target sets",
                    requirement.association,
                )
        complete = len(associations) == len(links) and bool(links)
    except LimitExceeded as error:
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.read_unverified"
                    if isinstance(error, ResourceError)
                    else "xkos.input_invalid",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return Correspondence._owned(
        identifier,
        types,
        compares,
        links,
        tuple(associations),
        source_release,
        target_release,
        prepared,
        selected,
        Report(
            "xkos.correspondence",
            len(associations),
            tuple(issues),
            requested=max(1, len(links)),
            complete=complete,
            regime="selected-grouped-correspondence",
            children=tuple(children),
        ),
        metadata,
        releases,
    )
