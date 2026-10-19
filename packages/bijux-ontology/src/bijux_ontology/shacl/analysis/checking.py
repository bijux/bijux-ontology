"""Finite supported constraint proofs over one dependency-aware shape profile."""

from dataclasses import dataclass
from typing import Literal as Kind

from rdflib import BNode, Graph, URIRef
from rdflib.namespace import RDF, SH
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list, strong_components
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.parameters.reading import ParameterReader, count_value
from bijux_ontology.shacl.profiles.references import ReferenceReader
from bijux_ontology.shacl.profiles.types import ShapeDependency, ShapeProfile, Triple

from .types import (
    LocalConflict,
    RootConflict,
    ShapeAnalysis,
    ShapeComponent,
    SignedDependency,
)


@dataclass
class _Shape:
    """Only established local prerequisites used by finite implication rules."""

    active: bool = True
    valid: bool = True
    property_shape: bool = False
    presence: bool = True


@dataclass(frozen=True)
class _Proof:
    """A bounded shared derivation, referring to original local facts and edges."""

    local: frozenset[Node]
    edges: frozenset[int] = frozenset()


class _Analysis:
    """Own one operation's bounded metadata, signed graph and retained proofs."""

    def __init__(self, graph: Graph, budget: Budget) -> None:
        """Reuse the profile's owned graph without copying or building an RDF index."""
        self.budget = budget
        self.parameters = ParameterReader(graph, budget)
        self.issues = IssueCollector(budget)
        self.local: list[LocalConflict] = []
        self.shapes: dict[Node, _Shape] = {}
        self.edges: list[SignedDependency] = []
        self.components: list[ShapeComponent] = []
        self.roots: list[RootConflict] = []
        self.completed = 0

    def count(
        self, shape: Node, predicate: URIRef
    ) -> tuple[int | None, tuple[Triple, ...]]:
        """Keep exact cardinality witnesses beside the static proof policy."""
        value = self.parameters.one(shape, predicate)
        if value is None:
            return None, ()
        return count_value(value, self.budget), ((shape, predicate, value),)

    def conflict(
        self,
        shape: Node,
        reason: Kind["count_bounds", "fixed_count", "fixed_allowed", "allowed_count"],
        witnesses: tuple[Triple, ...],
        allowed: tuple[Node, ...] | None = None,
    ) -> None:
        """Retain one direct proof independently of optional invocation context."""
        self.budget.consume("states", 1 + len(witnesses) + len(allowed or ()))
        self.local.append(LocalConflict(shape, reason, witnesses, allowed))

    def scan(self, shape: Node) -> None:
        """Check direct local cardinality/fixed-value/enumeration constraints only."""
        state = _Shape()
        self.budget.consume("states")
        self.shapes[shape] = state
        try:
            state.active = not self.parameters.boolean(shape, SH.deactivated)
            if not state.active:
                self.completed += 1
                return
            paths = self.parameters.values(shape, SH.path, single=True)
            kinds = self.parameters.values(shape, RDF.type)
            state.property_shape = bool(paths)
            if (
                SH.PropertyShape in kinds
                and not paths
                or SH.NodeShape in kinds
                and paths
            ):
                raise InputError("SHACL shape role and property path conflict")
            minimum, min_witness = self.count(shape, SH.minCount)
            maximum, max_witness = self.count(shape, SH.maxCount)
            if not state.property_shape and (
                minimum is not None or maximum is not None
            ):
                raise InputError("SHACL count parameters require a property shape")
            fixed = self.parameters.values(shape, SH.hasValue)
            self.budget.consume("states", len(fixed))
            fixed_keys = {term_key(value) for value in fixed}
            fixed_witness = tuple((shape, SH.hasValue, value) for value in fixed)
            lower = max(minimum or 0, len(fixed_keys))
            state.presence = not state.property_shape or lower > 0
            if minimum is not None and maximum is not None and minimum > maximum:
                self.conflict(shape, "count_bounds", min_witness + max_witness)
            if maximum is not None and len(fixed_keys) > maximum:
                self.conflict(shape, "fixed_count", fixed_witness + max_witness)
            heads = self.parameters.values(shape, SH["in"], single=True)
            if heads:
                allowed = set()
                allowed_values = []
                witnesses: list[Triple] = [(shape, SH["in"], heads[0])]
                if (RDF.nil, RDF.first, None) in self.parameters.graph or (
                    RDF.nil,
                    RDF.rest,
                    None,
                ) in self.parameters.graph:
                    raise InputError("SHACL rdf:nil must not have list structure")
                for value in iter_rdf_list(
                    self.parameters.graph,
                    heads[0],
                    max_items=self.budget.limits.max_states,
                    budget=self.budget,
                ):
                    self.budget.consume("states", 2)
                    key = term_key(value)
                    if key not in allowed:
                        allowed.add(key)
                        allowed_values.append(value)
                if fixed_keys - allowed:
                    self.conflict(
                        shape,
                        "fixed_allowed",
                        tuple(witnesses) + fixed_witness,
                        tuple(allowed_values),
                    )
                required = lower if state.property_shape else max(1, len(fixed_keys))
                if required > len(allowed):
                    self.conflict(
                        shape,
                        "allowed_count",
                        tuple(witnesses) + min_witness + fixed_witness,
                        tuple(allowed_values),
                    )
            if not state.property_shape and len(fixed_keys) > 1:
                self.conflict(shape, "fixed_count", fixed_witness)
            self.completed += 1
        except InputError as error:
            if isinstance(error, LimitExceeded):
                raise
            state.valid = False
            bound_node(shape, self.budget)
            self.issues.append(
                Issue(
                    "shacl.analysis_metadata_unverified",
                    str(error),
                    str(shape),
                    kind="unverified",
                )
            )

    def signed(self, dependency: ShapeDependency) -> SignedDependency:
        """Classify only justified reference polarity and retained active scope."""
        polarity: Kind["positive", "negative", "nonmonotone"] = "positive"
        if dependency.parameter == SH["not"]:
            polarity = "negative"
        elif dependency.parameter == SH.xone:
            polarity = "nonmonotone"
        elif (
            dependency.parameter == SH.qualifiedValueShape
            and self.shapes[dependency.shape].active
            and self.shapes[dependency.shape].valid
            and self.shapes[dependency.dependency].active
            and self.shapes[dependency.dependency].valid
        ):
            maximum, _ = self.count(dependency.shape, SH.qualifiedMaxCount)
            disjoint = self.parameters.boolean(
                dependency.shape, SH.qualifiedValueShapesDisjoint
            )
            if maximum is not None:
                polarity = "nonmonotone"
            if disjoint:
                polarity = "nonmonotone"
                self.issues.append(
                    Issue(
                        "shacl.analysis_qualification_unverified",
                        "Qualified sibling exclusions require additional "
                        "semantic dependencies",
                        str(dependency.shape),
                        kind="unverified",
                    )
                )
        source, target = (
            self.shapes[dependency.shape],
            self.shapes[dependency.dependency],
        )
        return SignedDependency(
            dependency,
            polarity,
            source.active and target.active and source.valid and target.valid,
        )


def _components(analysis: _Analysis) -> None:
    """Compute active signed SCCs iteratively without assigning recursive truth."""
    budget = analysis.budget
    nodes = analysis.shapes
    budget.consume("states", 2 * len(nodes))
    forward: dict[Node, list[Node]] = {node: [] for node in nodes}
    backward: dict[Node, list[Node]] = {node: [] for node in nodes}
    for edge in analysis.edges:
        budget.consume("work")
        if edge.active:
            budget.consume("states", 2)
            forward[edge.dependency.shape].append(edge.dependency.dependency)
            backward[edge.dependency.dependency].append(edge.dependency.shape)
    assigned: dict[Node, int] = {}
    groups: list[list[Node]] = []
    for group in strong_components(
        nodes, forward, backward, budget=budget, max_depth=budget.limits.max_depth
    ):
        budget.consume("states", len(group))
        budget.consume("work", len(group))
        budget.reserve_storage(64 * len(group))
        number = len(groups)
        groups.append(group)
        assigned.update((node, number) for node in group)
    budget.consume("states", len(groups))
    internal: list[list[SignedDependency]] = [[] for _ in groups]
    for edge in analysis.edges:
        budget.consume("work")
        if (
            edge.active
            and assigned[edge.dependency.shape] == assigned[edge.dependency.dependency]
        ):
            budget.consume("states")
            internal[assigned[edge.dependency.shape]].append(edge)
    for group, edges in zip(groups, internal, strict=True):
        budget.consume("work", len(edges))
        budget.consume("states", 1 + len(group) + len(edges))
        recursive = len(group) > 1 or any(
            e.dependency.shape == e.dependency.dependency for e in edges
        )
        negative = any(e.polarity != "positive" for e in edges)
        boundary: Kind[
            "acyclic", "processor_defined", "negative_recursion_unsupported"
        ] = (
            "negative_recursion_unsupported"
            if recursive and negative
            else "processor_defined"
            if recursive
            else "acyclic"
        )
        component = ShapeComponent(tuple(group), tuple(edges), recursive, boundary)
        analysis.components.append(component)
        if recursive:
            analysis.issues.append(
                Issue(
                    "shacl.analysis_recursion_unverified",
                    "Recursive shape validation has no assigned semantics "
                    "in this analysis",
                    str(group[0]),
                    actual=boundary,
                    kind="unverified",
                )
            )


def _proofs(analysis: _Analysis, roots: tuple[Node, ...]) -> None:
    """Propagate only finite necessary invocations; alternatives are never fused."""
    budget = analysis.budget
    proofs: dict[Node, _Proof] = {}
    by_shape: dict[Node, dict[URIRef, list[int]]] = {}
    for conflict in analysis.local:
        budget.consume("work")
        if analysis.shapes[conflict.shape].valid:
            budget.consume("states")
            proofs[conflict.shape] = _Proof(frozenset((conflict.shape,)))
    for index, edge in enumerate(analysis.edges):
        budget.consume("work")
        source = analysis.shapes[edge.dependency.shape]
        if source.active and source.valid:
            budget.consume("states", 2)
            by_shape.setdefault(edge.dependency.shape, {}).setdefault(
                edge.dependency.parameter, []
            ).append(index)
    changed = True
    while changed:
        changed = False
        for node, state in analysis.shapes.items():
            budget.consume("work")
            if (
                node in proofs
                or not state.active
                or not state.valid
                or not state.presence
            ):
                continue
            needed: list[int] = []
            for parameter, indexes in by_shape.get(node, {}).items():
                budget.consume("work", len(indexes))
                proven = [
                    index
                    for index in indexes
                    if analysis.edges[index].active
                    and analysis.edges[index].dependency.dependency in proofs
                ]
                if parameter in (SH.property, SH.node, SH["and"]) and proven:
                    needed = proven[:1]
                    break
                if parameter in (SH["or"], SH.xone) and len(proven) == len(indexes):
                    heads = analysis.parameters.values(node, parameter, single=True)
                    if heads:
                        needed = proven
                        break
            if not needed:
                continue
            local: set[Node] = set()
            dependencies: set[int] = set()
            for index in needed:
                child = proofs[analysis.edges[index].dependency.dependency]
                budget.consume("work", len(child.local) + len(child.edges))
                budget.consume("states", 1 + len(child.local) + len(child.edges))
                local.update(child.local)
                dependencies.update(child.edges)
                dependencies.add(index)
            proofs[node] = _Proof(frozenset(local), frozenset(dependencies))
            changed = True
    for root in roots:
        budget.consume("work")
        if root in proofs:
            proof = proofs[root]
            budget.consume("states", 1 + len(proof.local) + len(proof.edges))
            root_conflict = RootConflict(
                root,
                tuple(proof.local),
                tuple(analysis.edges[i].dependency for i in proof.edges),
            )
            analysis.roots.append(root_conflict)
            analysis.issues.append(
                Issue(
                    "shacl.static_incompatibility",
                    "Supported necessary shape constraints cannot hold together "
                    "for an invocation",
                    str(root),
                )
            )


def analyze_shape_profile(
    profile: ShapeProfile,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ShapeAnalysis:
    """Analyze finite local constraints and active dependencies without loading data.

    Structural extraction is the required preparation; its supplied-case behavior
    report is independent of these corpus-free checks. Local contradiction
    records are conditional on invoking that shape. Only necessary conflicts on
    explicitly selected roots become finding diagnostics. No conflict found is
    never a general satisfiability certificate. Recursion remains explicitly
    processor-defined, including positive cycles.
    """
    if not isinstance(profile, ShapeProfile):
        raise InputError("Shape analysis requires a dependency-aware ShapeProfile")
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Shape analysis requires typed limits and budget")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Shape analysis limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    analysis = _Analysis(Graph(), operation)
    roots: tuple[Node, ...] = ()
    complete = False
    stages = 0
    try:
        if type(profile.roots) is not tuple or type(profile.dependencies) is not tuple:
            raise InputError("Shape profile inventories require immutable tuples")
        if not isinstance(profile.structural_report, Report) or (
            profile.source is not None
            and not isinstance(profile.source, PreparedSource)
        ):
            raise InputError(
                "Shape profiles require typed source and structural report"
            )
        operation.consume("states", len(profile.roots) + len(profile.dependencies))
        retain_report(profile.structural_report, operation, analysis.issues)
        if (
            not profile.structural_report.verified
            or profile.source is None
            or not profile.roots
        ):
            analysis.issues.append(
                Issue(
                    "shacl.analysis_profile_unverified",
                    "Analysis requires completed selected structural extraction",
                    kind="unverified",
                )
            )
        else:
            analysis.parameters.graph = profile.source._read(budget=operation)
            # Qualify the existing selected RDF scalars before maintained list
            # traversal or dictionary lookups can copy their lexical metadata.
            owned_blanks: set[int] = set()
            for triple in analysis.parameters.graph:
                operation.consume("work")
                operation.consume("triples")
                for term in triple:
                    bound_node(term, operation)
                    if isinstance(term, BNode) and id(term) not in owned_blanks:
                        operation.consume("states")
                        owned_blanks.add(id(term))
            nodes = ReferenceReader(analysis.parameters, owned_blanks).qualify(
                profile.roots, profile.dependencies
            )
            roots = profile.roots
            for selected_node in nodes:
                operation.consume("work")
                analysis.scan(selected_node)
            for dependency in profile.dependencies:
                operation.consume("work")
                operation.consume("states")
                try:
                    analysis.edges.append(analysis.signed(dependency))
                except InputError as error:
                    if isinstance(error, LimitExceeded):
                        raise
                    analysis.issues.append(
                        Issue(
                            "shacl.analysis_metadata_unverified",
                            str(error),
                            str(dependency.shape),
                            kind="unverified",
                        )
                    )
            _components(analysis)
            stages += 1
            _proofs(analysis, roots)
            stages += 1
            complete = not any(
                issue.kind in ("input_error", "unverified", "exhausted")
                for issue in analysis.issues
            )
    except LimitExceeded as error:
        analysis.issues.exhausted(str(error), location="shacl.analysis")
    except InputError as error:
        try:
            analysis.issues.append(
                Issue(
                    "shacl.analysis_metadata_unverified", str(error), kind="unverified"
                )
            )
        except LimitExceeded as limit:
            analysis.issues.exhausted(str(limit), location="shacl.analysis")
    checked = analysis.completed + stages
    return ShapeAnalysis(
        tuple(analysis.local),
        tuple(analysis.roots),
        tuple(analysis.edges),
        tuple(analysis.components),
        Report(
            "shacl.analysis",
            checked,
            tuple(analysis.issues),
            requested=max(checked, len(analysis.shapes) + 2, 1),
            complete=complete,
            regime="finite-static-analysis",
        ),
        "impossible" if analysis.roots else "not_determined",
        tuple(node for node, state in analysis.shapes.items() if not state.active),
    )
