"""Processor-confirmed source node traits and nested model contribution identity."""

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.checking import PreparedUsages
from bijux_ontology.jsonld.entities import EntityKey, subject_key
from bijux_ontology.jsonld.observations import ObservedOccurrence, SourcePath
from bijux_ontology.jsonld.values import ValueTraits, identifier_kind
from bijux_ontology.limits import Budget


def source_node_traits(
    node: dict[str, object],
    path: SourcePath,
    prepared: PreparedUsages,
    budget: Budget,
) -> tuple[ValueTraits, ...]:
    """Read a selected subject's own keyword usages without context guessing.

    A selected record can have no incoming RDF edge, such as an @graph entry.
    Its property values do not identify its own types. Keyword spellings and
    aliases are established by the existing usage processor. A wrapper or a
    genuinely ambiguous derivative never acquires the contained child's traits.
    """
    if not path:
        return prepared.root_nodes
    observed = prepared.observe(path)
    if observed.limitation:
        return ()
    if "@value" in node:
        return tuple(item.traits for item in observed.occurrences)
    identifier = None
    types: list[str] = []
    own_keyword = False
    subjects: dict[tuple[object, ...], str | None] = {}
    unresolved = False
    for key, raw in node.items():
        budget.consume("work")
        if key == "@context" or not (
            isinstance(raw, str)
            or isinstance(raw, list)
            and all(isinstance(item, str) for item in raw)
        ):
            continue
        try:
            usage = prepared.observe((*path, key))
        except LimitExceeded:
            raise
        except InputError:
            unresolved = True
            continue
        if usage.limitation:
            unresolved = True
        for item in usage.occurrences:
            budget.consume("work")
            if item.predicate == "@id":
                identifier = item.traits.identifier
                own_keyword = True
            elif item.predicate == "@type" and item.traits.identifier is not None:
                types.append(item.traits.identifier)
                identifier = identifier or item.subject
                own_keyword = True
            elif not item.predicate.startswith("@"):
                budget.consume("work", len(observed.occurrences))
                if any(
                    item.depth > edge.depth
                    and item.subject == edge.traits.identifier
                    and item.subject_identity == edge.identifier_identity
                    and item.graph == edge.graph
                    and item.graph_identity == edge.graph_identity
                    and item.anonymous_graph == edge.anonymous_graph
                    for edge in observed.occurrences
                ):
                    subjects[
                        (
                            item.subject,
                            item.subject_identity,
                            item.graph,
                            item.graph_identity,
                            item.anonymous_graph,
                        )
                    ] = item.subject
    if unresolved:
        return ()
    if own_keyword:
        return (
            ValueTraits(
                "node",
                identifier=identifier,
                identifier_kind=identifier_kind(identifier),
                types=tuple(dict.fromkeys(types)),
            ),
        )
    if len(subjects) == 1:
        subject = next(iter(subjects.values()))
        return (
            ValueTraits(
                "node", identifier=subject, identifier_kind=identifier_kind(subject)
            ),
        )
    return ()


def same_contribution(left: ObservedOccurrence, right: ObservedOccurrence) -> bool:
    """Match identities only within the same prepared usage expansion.

    Differential selection intentionally ignores local anonymous tokens. Binding
    coverage additionally keeps those identities and distinct list occurrences.
    """
    return (
        left.identity == right.identity
        and left.subject_identity == right.subject_identity
        and left.graph_identity == right.graph_identity
        and left.identifier_identity == right.identifier_identity
        and left.list_id == right.list_id
        and left.list_position == right.list_position
    )


def source_entity(
    node: dict[str, object],
    path: SourcePath,
    prepared: PreparedUsages,
    budget: Budget,
) -> EntityKey | None:
    """Associate a source subject with its established graph, never by raw IDs."""
    keys: set[EntityKey] = set()
    own_keys: set[EntityKey] = set()
    for key, raw in node.items():
        budget.consume("work")
        if key == "@context":
            continue
        try:
            usage = prepared.observe((*path, key))
        except LimitExceeded:
            raise
        except InputError:
            continue
        if usage.limitation or not usage.occurrences:
            continue
        depth = min(item.depth for item in usage.occurrences)
        for item in usage.occurrences:
            budget.consume("work")
            if item.depth != depth:
                continue
            primitive = (
                isinstance(raw, str)
                or isinstance(raw, list)
                and all(isinstance(value, str) for value in raw)
            )
            if item.predicate == "@id" and primitive:
                own_keys.add(
                    EntityKey(
                        item.traits.identifier,
                        graph=item.graph,
                        anonymous_graph=item.anonymous_graph,
                        graph_identity=item.graph_identity,
                    )
                )
            elif item.predicate == "@type" and primitive:
                own_keys.add(subject_key(item))
            elif not item.predicate.startswith("@"):
                keys.add(subject_key(item))
    selected = own_keys or keys
    return next(iter(selected)) if len(selected) == 1 else None
