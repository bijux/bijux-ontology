"""Reuse original typed closure and static analysis for finite construction."""

from dataclasses import dataclass

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.limits import Budget, IssueCollector, retain_report
from bijux_ontology.rdf.paths import RdfPath
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.analysis.checking import analyze_shape_profile
from bijux_ontology.shacl.analysis.types import ShapeAnalysis
from bijux_ontology.shacl.authoring.types import (
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclPropertyShape,
    ShaclShapeRef,
    ShaclValue,
)
from bijux_ontology.shacl.inspection.reading import inspect_shapes
from bijux_ontology.shacl.inspection.types import ShapeInspection

from .types import MissingValueExample

Definition = ShaclNodeShape | ShaclPropertyShape
Value = URIRef | Literal
Triple = tuple[Node, Node, Node]
_FACETS = frozenset(
    (
        SH.minInclusive,
        SH.maxInclusive,
        SH.minExclusive,
        SH.maxExclusive,
        SH.minLength,
        SH.maxLength,
    )
)


class UnsupportedExample(ResourceError):
    """The supplied contract exceeds finite construction's supported semantics."""


def reserve(budget: Budget, count: int = 1) -> None:
    """Reserve owned graph/state/reference storage before materializing a branch."""
    budget.consume("states", count)
    budget.consume("bytes", 192 * count)


@dataclass(frozen=True)
class PreparedExamples:
    """Original source receipts plus reused typed definitions and explicit values."""

    inspection: ShapeInspection
    analysis: ShapeAnalysis
    definitions: dict[URIRef | BNode, Definition]
    pool: tuple[Value, ...]
    seed: tuple[Triple, ...]
    shapes_metadata: SnapshotMetadata
    seed_metadata: SnapshotMetadata
    negative_path: URIRef | None


def physical(source: PreparedSource, budget: Budget) -> tuple[Graph, SnapshotMetadata]:
    """Keep one original physical graph and exact anonymous selection authority."""
    metadata = SnapshotMetadata.capture(source, budget)
    if (
        metadata.union
        or metadata.graph_fidelity != "complete"
        or metadata.literal_fidelity not in ("source_terms", "supplied_terms")
    ):
        raise UnsupportedExample(
            "Examples require one complete physical graph with exact RDF terms"
        )
    if isinstance(metadata.graph_name, BNode) and not any(
        name is metadata.graph_name for name in source._graph_names(budget=budget)
    ):
        raise InputError("A blank graph pin requires exact owned source identity")
    contexts = source._contexts(budget=budget)
    _, _, graph = next(contexts)
    if next(contexts, None) is not None:
        raise UnsupportedExample("Select exactly one physical example graph")
    return graph, metadata


def prepare(
    shapes: PreparedSource,
    root: URIRef | BNode,
    focus: URIRef,
    seed: PreparedSource,
    pool: tuple[Value, ...],
    negative: MissingValueExample | None,
    budget: Budget,
    issues: IssueCollector,
    receipts: list[Report],
) -> PreparedExamples:
    """Account for every selected semantic obligation before searching candidates."""
    if (
        not isinstance(shapes, PreparedSource)
        or not isinstance(seed, PreparedSource)
        or not isinstance(root, URIRef | BNode)
        or type(focus) is not URIRef
    ):
        raise InputError("Use owned shapes/seed, an original shape and an IRI focus")
    bound_node(root, budget)
    bound_node(focus, budget)
    _, shapes_metadata = physical(shapes, budget)
    data, seed_metadata = physical(seed, budget)
    seed_rows = []
    present = False
    reserve(budget, len(data))
    for triple in data:
        budget.consume("work")
        budget.consume("triples")
        for node in triple:
            bound_node(node, budget)
        present = present or triple[0] == focus or triple[2] == focus
        seed_rows.append(triple)
    if not present or not seed_rows:
        raise InputError(
            "Supply a nonempty seed retaining the explicit focus and its anchor facts"
        )
    if type(pool) is not tuple:
        raise InputError("Candidate values require an immutable IRI/literal tuple")
    reserve(budget, len(pool))
    owned: dict[tuple[str | None, ...], Value] = {}
    for value in pool:
        if type(value) not in (URIRef, Literal):
            raise InputError(
                "Example pools contain original named IRIs or literals only"
            )
        bound_node(value, budget)
        reserve(budget)
        owned.setdefault(term_key(value), value)
    reserve(budget, 3)
    inspection = inspect_shapes(shapes, (root,), budget=budget)
    receipts.extend((inspection.report, inspection.profile.report))
    retain_report(inspection.profile.structural_report, budget, issues)
    analysis = analyze_shape_profile(inspection.profile, budget=budget)
    receipts.append(analysis.report)
    # Analysis proves only local/necessary contradictions. Do not turn a bad OR
    # alternative into a rejection of a separate supported viable alternative.
    retain_report(analysis.report, budget, issues)
    if (
        not inspection.profile.structural_report.verified
        or not analysis.report.complete
        or any(item.recursive for item in analysis.components)
    ):
        raise UnsupportedExample(
            "Incomplete or recursive shape closure cannot qualify an example"
        )
    if any(item.predicate not in _FACETS for item in inspection.remainder):
        raise UnsupportedExample(
            "Selected queries, paths or semantic extensions remain unsupported"
        )
    if any(
        issue.kind in ("input_error", "exhausted") for issue in inspection.report.issues
    ):
        raise UnsupportedExample("Typed shape inspection did not complete")
    reserve(budget, len(inspection.definitions))
    definitions = {item.identifier: item for item in inspection.definitions}
    reserve(budget, len(inspection.profile.dependencies) * 2 + 1)
    closure: set[Node] = {root}
    for dependency in inspection.profile.dependencies:
        budget.consume("work")
        closure.update((dependency.shape, dependency.dependency))
    if set(definitions) != closure or not isinstance(
        definitions.get(root), ShaclNodeShape
    ):
        raise UnsupportedExample("A complete typed node-shape closure is required")
    for definition in definitions.values():
        budget.consume("work")
        if isinstance(definition, ShaclPropertyShape) and (
            not isinstance(definition.path, RdfPath)
            or definition.path.kind != "predicate"
            or not isinstance(definition.path.node, URIRef)
        ):
            raise UnsupportedExample(
                "Example construction supports direct predicate paths only"
            )
        for constraint in definition.constraints:
            budget.consume("work")
            if isinstance(constraint, ShaclLogical):
                if constraint.operator not in ("and", "or"):
                    raise UnsupportedExample(
                        "Only finite AND/OR branches are supported"
                    )
            elif not isinstance(constraint, ShaclCount | ShaclValue | ShaclShapeRef):
                raise UnsupportedExample(
                    "Qualified counts/comparisons exceed finite construction"
                )
    negative_path = None
    if negative is not None:
        if (
            type(negative) is not MissingValueExample
            or type(negative.shape) is not URIRef
            or type(negative.preserve) is not tuple
            or any(type(item) is not URIRef for item in negative.preserve)
        ):
            raise InputError(
                "Missing-value requests require original named direct property shapes"
            )
        bound_node(negative.shape, budget)
        for selected in negative.preserve:
            bound_node(selected, budget)
        root_definition = definitions[root]
        assert isinstance(root_definition, ShaclNodeShape)
        reserve(budget, len(root_definition.properties) + len(negative.preserve))
        direct = {item.identifier for item in root_definition.properties}
        if (
            negative.shape not in direct
            or any(
                item not in direct or item == negative.shape
                for item in negative.preserve
            )
            or len(set(negative.preserve)) != len(negative.preserve)
        ):
            raise UnsupportedExample(
                "Name distinct original direct-root counterexample properties"
            )
        target = definitions[negative.shape]
        if not isinstance(target, ShaclPropertyShape) or not any(
            isinstance(item, ShaclCount) and (item.minimum or 0) > 0
            for item in target.constraints
        ):
            raise UnsupportedExample(
                "A missing-value counterexample requires an actual positive MinCount"
            )
        assert isinstance(target.path, RdfPath)
        assert isinstance(target.path.node, URIRef)
        negative_path = target.path.node
    return PreparedExamples(
        inspection,
        analysis,
        definitions,
        tuple(owned.values()),
        tuple(seed_rows),
        shapes_metadata,
        seed_metadata,
        negative_path,
    )
