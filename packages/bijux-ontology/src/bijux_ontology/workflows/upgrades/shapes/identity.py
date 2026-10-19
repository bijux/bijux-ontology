"""Named finding identities and bounded semantic path keys, never blank labels."""

from collections import defaultdict
from collections.abc import Sequence

from rdflib import BNode, Graph, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.paths.types import RdfPath
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.evidence.findings import ShaclFinding

from .types import ShapeCorrespondence

ShapeKey = tuple[str, str]
FindingKey = tuple[object, ...]


def shape_maps(
    pairs: Sequence[object], graphs: tuple[Graph, Graph], budget: Budget
) -> tuple[dict[URIRef, ShapeKey], dict[URIRef, ShapeKey]]:
    """Qualify finite caller correspondences against their actual selected graphs."""
    maps: tuple[dict[URIRef, ShapeKey], dict[URIRef, ShapeKey]] = ({}, {})
    names: set[str] = set()
    for pair in pairs:
        if not isinstance(pair, ShapeCorrespondence):
            raise InputError("Expected typed shape correspondence")
        bound_text(pair.identifier, budget)
        if pair.identifier in names:
            raise InputError("Shape correspondence names must be unique")
        budget.consume("states")
        names.add(pair.identifier)
        for side, endpoint in enumerate((pair.before, pair.after)):
            budget.consume("work")
            if endpoint is not None:
                bound_node(endpoint, budget)
                if endpoint in maps[side] or (endpoint, None, None) not in graphs[side]:
                    raise InputError("Shape correspondence endpoint is absent/reused")
                budget.consume("states")
                maps[side][endpoint] = ("declared", pair.identifier)
    return maps


def shape_key(node: URIRef, mapping: dict[URIRef, ShapeKey]) -> ShapeKey:
    """Equal named IRIs retain identity unless explicit correspondence replaces it."""
    return mapping.get(node, ("iri", str(node)))


def path_key(path: RdfPath | None, budget: Budget) -> tuple[object, ...] | None:
    """Ignore source tokens; retain sequence order and unordered alternative choice."""
    if path is None:
        return None
    budget.consume("work")
    budget.consume("states")
    bound_text(path.kind, budget)
    if path.limitation is not None:
        raise InputError("Finding path meaning is incomplete")
    if path.kind == "predicate":
        if not isinstance(path.node, URIRef) or path.children:
            raise InputError("Finding predicate path is not established")
        bound_node(path.node, budget)
        return path.kind, str(path.node)
    children = tuple(path_key(child, budget) for child in path.children)
    if path.kind == "alternative":
        children = tuple(sorted(set(children), key=repr))
    return path.kind, children


def finding_groups(
    findings: tuple[ShaclFinding, ...],
    graph: Graph,
    mapping: dict[URIRef, ShapeKey],
    budget: Budget,
) -> tuple[dict[FindingKey, list[ShaclFinding]], tuple[ShaclFinding, ...]]:
    """Only established named source shapes and nonblank data terms form keys."""
    groups: dict[FindingKey, list[ShaclFinding]] = defaultdict(list)
    unknown = []
    for finding in findings:
        budget.consume("work")
        budget.consume("states")
        for node in (
            finding.focus_node,
            finding.source_shape,
            finding.constraint_component,
            finding.value,
            finding.severity,
        ):
            if node is not None:
                bound_node(node, budget)
        if (
            finding.focus_node is None
            or isinstance(finding.focus_node, BNode)
            or isinstance(finding.value, BNode)
            or isinstance(finding.severity, BNode)
            or not isinstance(finding.source_shape, URIRef)
            or not isinstance(finding.constraint_component, URIRef)
            or (finding.source_shape, None, None) not in graph
        ):
            unknown.append(finding)
            continue
        key = (
            term_key(finding.focus_node),
            path_key(finding.path_expression, budget),
            str(finding.constraint_component),
            shape_key(finding.source_shape, mapping),
            term_key(finding.value) if finding.value is not None else None,
            term_key(finding.severity) if finding.severity is not None else None,
        )
        groups[key].append(finding)
    return groups, tuple(unknown)
