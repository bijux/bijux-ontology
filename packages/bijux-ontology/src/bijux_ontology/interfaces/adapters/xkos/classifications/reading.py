"""One exact physical RDF scope for finite XKOS level and category projections."""

from threading import Event

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import SKOS
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report

from .types import (
    XKOS as X,
)
from .types import (
    XKOS_VERSION,
    CategoryRow,
    ClassificationLevel,
    ClassificationView,
    HierarchyLink,
    RelationKind,
    Resource,
)

_RELATIONS: tuple[tuple[URIRef, RelationKind, bool], ...] = (
    (X.specializes, "generic", False),
    (X.generalizes, "generic", True),
    (X.isPartOf, "partitive", False),
    (X.hasPart, "partitive", True),
    (SKOS.broader, "skos", False),
    (SKOS.narrower, "skos", True),
)


def reserve(budget: Budget, count: int = 1) -> None:
    """Prepay retained immutable records and their bounded reference vectors."""
    budget.consume("states", count)
    budget.consume("bytes", 192 * count)


def values(
    graph: Graph, subject: Node, predicate: URIRef, budget: Budget
) -> tuple[Node, ...]:
    """Read actual terms before constructing a finite retained field inventory."""
    result = []
    for term in graph.objects(subject, predicate):
        budget.consume("work")
        bound_node(term, budget)
        reserve(budget)
        result.append(term)
    return tuple(result)


def _selected(
    items: tuple[Resource, ...] | None, owned: set[int], operation: Budget
) -> tuple[Resource, ...] | None:
    """Qualify every selector before hashing; blank labels prove no ownership."""
    if items is None:
        return None
    if type(items) is not tuple:
        raise InputError("Classification selections require finite resource tuples")
    reserve(operation, len(items))
    result: list[Resource] = []
    seen: set[Resource] = set()
    for item in items:
        operation.consume("work")
        if not isinstance(item, URIRef | BNode):
            raise InputError("Classification selections require RDF resources")
        bound_node(item, operation)
        if isinstance(item, BNode) and id(item) not in owned:
            raise ResourceError("Blank selector lacks selected source ownership")
        if item in seen:
            raise InputError("Select each classification resource once")
        seen.add(item)
        result.append(item)
    return tuple(result)


def read_classification(
    source: PreparedSource | Graph,
    scheme: Resource,
    *,
    version: str = XKOS_VERSION,
    graph_name: GraphSelection | None = None,
    levels: tuple[Resource, ...] | None = None,
    categories: tuple[Resource, ...] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ClassificationView:
    """Inspect one supplied version-scheme and preserve selected level positions.

    The caller chooses the version-specific scheme resource; no family or release
    identity is inferred from labels. Level order comes from its actual RDF list.
    Membership uses level-to-category skos:member. Only selected levels/categories
    receive table rows; other classification schemes have no application duties.
    This raw inventory is separate from check_classification policy acceptance.
    """
    if not isinstance(scheme, URIRef | BNode):
        raise InputError("Select an exact version-scheme resource")
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    prepared = None
    physical = None
    metadata = None
    ordered: list[Resource] = []
    records: list[ClassificationLevel] = []
    rows: list[CategoryRow] = []
    relations: list[HierarchyLink] = []
    coverage = []
    declared_count: tuple[Node, ...] = ()
    selected_categories: tuple[Resource, ...] = ()
    complete = False
    try:
        bound_text(version, operation)
        if version != XKOS_VERSION:
            raise ResourceError("Unsupported XKOS classification specification profile")
        bound_node(scheme, operation)
        if not isinstance(source, PreparedSource | Graph):
            raise InputError("Classification reading requires supplied RDF")
        prepared = (
            source
            if isinstance(source, PreparedSource)
            else prepare_rdf(source, budget=operation)
        )
        metadata = SnapshotMetadata.capture(prepared, operation)
        if (
            prepared.union
            or prepared.graph_fidelity != "complete"
            or prepared.literal_fidelity not in ("source_terms", "supplied_terms")
        ):
            raise ResourceError(
                "Classification reading requires exact physical RDF scope"
            )
        if isinstance(graph_name, BNode) and not any(
            name is graph_name for name in prepared._graph_names(budget=operation)
        ):
            raise ResourceError("Blank graph selector lacks source ownership")
        contexts = prepared._contexts(graph_name, budget=operation)
        _, physical, graph = next(contexts)
        if next(contexts, None) is not None:
            raise ResourceError("Select one physical classification graph")
        owned: set[int] = set()
        for triple in graph:
            operation.consume("work")
            operation.consume("triples")
            for term in triple:
                bound_node(term, operation)
                if isinstance(term, BNode):
                    reserve(operation)
                    owned.add(id(term))
        if isinstance(scheme, BNode) and id(scheme) not in owned:
            raise ResourceError("Blank scheme selector lacks selected source ownership")
        chosen_levels = _selected(levels, owned, operation)
        chosen_categories = _selected(categories, owned, operation)
        heads = values(graph, scheme, X.levels, operation)
        if len(heads) != 1:
            raise ResourceError("Selected scheme requires one unambiguous levels list")
        try:
            for term in iter_rdf_list(
                graph, heads[0], max_items=operation.limits.max_states, budget=operation
            ):
                bound_node(term, operation)
                if not isinstance(term, URIRef | BNode):
                    raise ResourceError(
                        "Classification level list contains a non-resource"
                    )
                reserve(operation)
                ordered.append(term)
        except LimitExceeded:
            raise
        except InputError as error:
            issues.append(Issue("xkos.levels_list_invalid", str(error), str(scheme)))
            raise ResourceError(
                "Required ordered levels could not be fully inspected"
            ) from error
        reserve(operation, len(ordered))
        available = set(ordered)
        if chosen_levels is not None and any(
            level not in available for level in chosen_levels
        ):
            raise ResourceError(
                "A requested classification level is outside this scheme"
            )
        selected_set = set(chosen_levels) if chosen_levels is not None else available
        category_set = set(chosen_categories) if chosen_categories is not None else None
        actual_categories: dict[Resource, None] = {}
        support_categories: set[Resource] = set()
        for position, level in enumerate(ordered, 1):
            operation.consume("work")
            if level not in selected_set:
                continue
            depths = values(graph, level, X.depth, operation)
            members = values(graph, level, SKOS.member, operation)
            patterns = values(graph, level, X.notationPattern, operation)
            organized = values(graph, level, X.organizedBy, operation)
            for predicate in (
                X.covers,
                X.coversExhaustively,
                X.coversMutuallyExclusively,
            ):
                for term in values(graph, level, predicate, operation):
                    reserve(operation)
                    coverage.append((level, predicate, term))
            reserve(operation, 2)
            records.append(
                ClassificationLevel(
                    level, position, depths, members, patterns, organized
                )
            )
            for member in members:
                operation.consume("work")
                if not isinstance(member, URIRef | BNode):
                    continue
                reserve(operation)
                support_categories.add(member)
                if category_set is not None and member not in category_set:
                    continue
                reserve(operation, 2)
                actual_categories[member] = None
                codes = tuple(
                    value
                    for value in values(graph, member, SKOS.notation, operation)
                    if isinstance(value, Literal)
                )
                labels = tuple(
                    value
                    for value in values(graph, member, SKOS.prefLabel, operation)
                    if isinstance(value, Literal)
                )
                rows.append(CategoryRow(member, level, position, codes, labels))
        selected_categories = (
            chosen_categories
            if chosen_categories is not None
            else tuple(actual_categories)
        )
        for predicate, kind, inverse in _RELATIONS:
            for s, _, o in graph.triples((None, predicate, None)):
                operation.consume("work")
                child, parent = (o, s) if inverse else (s, o)
                for term in (s, o):
                    bound_node(term, operation)
                if child not in support_categories:
                    continue
                reserve(operation)
                relations.append(HierarchyLink(child, parent, kind, (s, predicate, o)))
        for predicate in (X.covers, X.coversExhaustively, X.coversMutuallyExclusively):
            for term in values(graph, scheme, predicate, operation):
                reserve(operation)
                coverage.append((scheme, predicate, term))
        declared_count = values(graph, scheme, X.numberOfLevels, operation)
        complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "xkos.classification_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return ClassificationView._owned(
        prepared,
        physical,
        scheme,
        metadata,
        tuple(ordered),
        tuple(records),
        selected_categories,
        categories is not None,
        tuple(rows),
        tuple(relations),
        tuple(coverage),
        declared_count,
        Report(
            "adapter.xkos.classification.read",
            len(records),
            tuple(issues),
            requested=len(records) if complete else len(records) + 1,
            allow_empty=True,
            complete=complete,
            regime=XKOS_VERSION,
        ),
    )
