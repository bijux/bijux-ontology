"""Finite supported constraint proofs over one dependency-aware shape profile."""

from dataclasses import dataclass
from typing import Literal as Kind

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.structure import iter_rdf_list
from bijux_ontology.rdf.terms import bound_node, term_key
from bijux_ontology.shacl.profiles.types import ShapeDependency, ShapeProfile, Triple

from .types import (
    LocalConflict,
    RootConflict,
    ShapeAnalysis,
    ShapeComponent,
    SignedDependency,
)

_REFERENCES = frozenset(
    (
        SH.property,
        SH.node,
        SH["not"],
        SH["and"],
        SH["or"],
        SH.xone,
        SH.qualifiedValueShape,
    )
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
        self.graph, self.budget = graph, budget
        self.issues = IssueCollector(budget)
        self.local: list[LocalConflict] = []
        self.shapes: dict[Node, _Shape] = {}
        self.edges: list[SignedDependency] = []
        self.components: list[ShapeComponent] = []
        self.roots: list[RootConflict] = []
        self.completed = 0
        self.owned_blanks: set[int] = set()

    def bound_metadata(self, node: Node) -> None:
        """Require actual snapshot ownership before anonymous equality lookups."""
        bound_node(node, self.budget)
        if isinstance(node, BNode) and id(node) not in self.owned_blanks:
            raise InputError(
                "Shape metadata blank terms require exact snapshot ownership"
            )

    def values(
        self, shape: Node, predicate: URIRef, *, single: bool = False
    ) -> tuple[Node, ...]:
        """Read only actual metadata and stop a single-valued field at two terms."""
        result = []
        for value in self.graph.objects(shape, predicate):
            self.budget.consume("work")
            bound_node(value, self.budget)
            self.budget.consume("states")
            result.append(value)
            if single and len(result) > 1:
                raise InputError("A selected SHACL parameter has multiple values")
        return tuple(result)

    def boolean(self, shape: Node, predicate: URIRef) -> bool:
        """Accept only well-formed explicit boolean metadata, without truth coercion."""
        values = self.values(shape, predicate, single=True)
        if not values:
            return False
        value = values[0]
        if not isinstance(value, Literal) or value.datatype != XSD.boolean:
            raise InputError("SHACL boolean metadata requires xsd:boolean")
        text = str(value)
        if text not in ("true", "false", "1", "0"):
            raise InputError("SHACL boolean metadata has an invalid lexical form")
        return text in ("true", "1")

    def count(
        self, shape: Node, predicate: URIRef
    ) -> tuple[int | None, tuple[Triple, ...]]:
        """Qualify finite nonnegative xsd:integer bounds before integer conversion."""
        values = self.values(shape, predicate, single=True)
        if not values:
            return None, ()
        value = values[0]
        if not isinstance(value, Literal) or value.datatype != XSD.integer:
            raise InputError("SHACL cardinality metadata requires xsd:integer")
        if len(value) > 4096:
            raise LimitExceeded(
                "SHACL cardinality metadata exceeds bounded integer precision"
            )
        self.budget.consume("work", len(value) + 1)
        if not check_literal(value, budget=self.budget).verified:
            raise InputError("SHACL cardinality metadata has invalid lexical text")
        number = int(value)
        if number < 0:
            raise InputError("SHACL cardinality metadata cannot be negative")
        return number, ((shape, predicate, value),)

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
            state.active = not self.boolean(shape, SH.deactivated)
            if not state.active:
                self.completed += 1
                return
            paths = self.values(shape, SH.path, single=True)
            kinds = self.values(shape, RDF.type)
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
            fixed = self.values(shape, SH.hasValue)
            self.budget.consume("states", len(fixed))
            fixed_keys = {term_key(value) for value in fixed}
            fixed_witness = tuple((shape, SH.hasValue, value) for value in fixed)
            lower = max(minimum or 0, len(fixed_keys))
            state.presence = not state.property_shape or lower > 0
            if minimum is not None and maximum is not None and minimum > maximum:
                self.conflict(shape, "count_bounds", min_witness + max_witness)
            if maximum is not None and len(fixed_keys) > maximum:
                self.conflict(shape, "fixed_count", fixed_witness + max_witness)
            heads = self.values(shape, SH["in"], single=True)
            if heads:
                allowed = set()
                allowed_values = []
                witnesses: list[Triple] = [(shape, SH["in"], heads[0])]
                if (RDF.nil, RDF.first, None) in self.graph or (
                    RDF.nil,
                    RDF.rest,
                    None,
                ) in self.graph:
                    raise InputError("SHACL rdf:nil must not have list structure")
                for value in iter_rdf_list(
                    self.graph,
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
            disjoint = self.boolean(dependency.shape, SH.qualifiedValueShapesDisjoint)
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


def _reference_inventory(
    analysis: _Analysis,
    nodes: dict[Node, None],
    dependencies: tuple[ShapeDependency, ...],
) -> None:
    """Verify the supplied complete dependency plan against actual owned assertions.

    The maintained list iterator establishes logical membership. Supplied witness
    prefixes only describe positions in that already qualified list; they never
    substitute a caller-authored parameter or endpoint for an RDF assertion.
    """
    budget = analysis.budget
    logical = (SH["and"], SH["or"], SH.xone)
    expected: dict[tuple[Node, URIRef, Node, int | None], Node] = {}
    for shape in nodes:
        budget.consume("work")
        for parameter in _REFERENCES:
            budget.consume("work")
            for anchor in analysis.values(
                shape, parameter, single=parameter in logical
            ):
                members = (
                    iter_rdf_list(
                        analysis.graph,
                        anchor,
                        max_items=budget.limits.max_states,
                        budget=budget,
                    )
                    if parameter in logical
                    else iter((anchor,))
                )
                for position, target in enumerate(members):
                    budget.consume("work")
                    bound_node(target, budget)
                    if not isinstance(target, URIRef | BNode) or target not in nodes:
                        raise InputError(
                            "Profile reference endpoint is absent "
                            "from its dependency inventory"
                        )
                    budget.consume("states")
                    expected[
                        (
                            shape,
                            parameter,
                            target,
                            position if parameter in logical else None,
                        )
                    ] = anchor
    seen = set()
    for dependency in dependencies:
        budget.consume("work")
        shape, parameter, target = (
            dependency.shape,
            dependency.parameter,
            dependency.dependency,
        )
        via = dependency.via
        witness_position = (len(via) - 3) // 2 if parameter in logical else None
        key = shape, parameter, target, witness_position
        if key not in expected or key in seen:
            raise InputError(
                "Profile dependency metadata differs "
                "from the actual reference inventory"
            )
        anchor = expected[key]
        if via[0] != (shape, parameter, anchor):
            raise InputError(
                "Profile dependency anchor does not match its source and parameter"
            )
        if parameter in logical:
            if len(via) < 3 or len(via) % 2 != 1:
                raise InputError(
                    "Logical dependencies require their exact list-position witness"
                )
            cell = anchor
            for index in range(1, len(via), 2):
                budget.consume("work")
                first, rest = via[index], via[index + 1]
                if first[:2] != (cell, RDF.first) or rest[:2] != (cell, RDF.rest):
                    raise InputError(
                        "Logical dependency witness is not a connected list prefix"
                    )
                cell = rest[2]
            if via[-2][2] != target:
                raise InputError(
                    "Logical dependency witness names a different endpoint"
                )
        elif len(via) != 1:
            raise InputError("Direct dependencies require their exact source assertion")
        budget.consume("states")
        seen.add(key)
    if len(seen) != len(expected):
        raise InputError(
            "Profile dependency inventory omits actual outgoing references"
        )


def _components(analysis: _Analysis) -> None:
    """Compute active signed SCCs iteratively without assigning recursive truth."""
    budget = analysis.budget
    nodes = analysis.shapes
    budget.consume("states", 2 * len(nodes))
    forward: dict[Node, list[int]] = {node: [] for node in nodes}
    backward: dict[Node, list[Node]] = {node: [] for node in nodes}
    for index, edge in enumerate(analysis.edges):
        budget.consume("work")
        if edge.active:
            budget.consume("states", 2)
            forward[edge.dependency.shape].append(index)
            backward[edge.dependency.dependency].append(edge.dependency.shape)
    seen = set()
    finished = []
    for seed in nodes:
        budget.consume("work")
        if seed in seen:
            continue
        stack = [(seed, False, 0)]
        while stack:
            node, exiting, depth = stack.pop()
            budget.consume("work")
            if exiting:
                budget.consume("states")
                finished.append(node)
                continue
            if node in seen:
                continue
            if depth > budget.limits.max_depth:
                raise LimitExceeded("Shape dependency depth exceeds max_depth")
            budget.consume("states", 2)
            seen.add(node)
            stack.append((node, True, depth))
            for index in forward[node]:
                budget.consume("work")
                child = analysis.edges[index].dependency.dependency
                if child not in seen:
                    budget.consume("states")
                    stack.append((child, False, depth + 1))
    assigned: dict[Node, int] = {}
    groups: list[list[Node]] = []
    for seed in reversed(finished):
        budget.consume("work")
        if seed in assigned:
            continue
        budget.consume("states")
        group: list[Node] = []
        number = len(groups)
        groups.append(group)
        pending = [(seed, 0)]
        while pending:
            node, depth = pending.pop()
            budget.consume("work")
            if node in assigned:
                continue
            if depth > budget.limits.max_depth:
                raise LimitExceeded("Shape dependency depth exceeds max_depth")
            budget.consume("states", 2)
            assigned[node] = number
            group.append(node)
            for parent in backward[node]:
                budget.consume("work")
                if parent not in assigned:
                    budget.consume("states")
                    pending.append((parent, depth + 1))
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
                    heads = analysis.values(node, parameter, single=True)
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
            analysis.graph = profile.source._read(budget=operation)
            # Qualify the existing selected RDF scalars before maintained list
            # traversal or dictionary lookups can copy their lexical metadata.
            for triple in analysis.graph:
                operation.consume("work")
                operation.consume("triples")
                for term in triple:
                    bound_node(term, operation)
                    if (
                        isinstance(term, BNode)
                        and id(term) not in analysis.owned_blanks
                    ):
                        operation.consume("states")
                        analysis.owned_blanks.add(id(term))
            nodes: dict[Node, None] = {}
            for node in profile.roots:
                if not isinstance(node, URIRef | BNode):
                    raise InputError("Selected shape roots require resource terms")
                analysis.bound_metadata(node)
                operation.consume("states")
                nodes[node] = None
            roots = profile.roots
            for dependency in profile.dependencies:
                operation.consume("work")
                if not isinstance(dependency, ShapeDependency):
                    raise InputError(
                        "Analysis requires exact supported shape-reference metadata"
                    )
                if (
                    not isinstance(dependency.shape, URIRef | BNode)
                    or not isinstance(dependency.dependency, URIRef | BNode)
                    or not isinstance(dependency.parameter, URIRef)
                    or type(dependency.via) is not tuple
                    or not dependency.via
                ):
                    raise InputError(
                        "Shape references require exact resource witnesses"
                    )
                for node in (
                    dependency.shape,
                    dependency.parameter,
                    dependency.dependency,
                ):
                    analysis.bound_metadata(node)
                if dependency.parameter not in _REFERENCES:
                    raise InputError(
                        "Analysis requires exact supported shape-reference metadata"
                    )
                operation.consume("states", 2 + len(dependency.via))
                for triple in dependency.via:
                    operation.consume("work")
                    if type(triple) is not tuple or len(triple) != 3:
                        raise InputError("Shape witnesses require original RDF triples")
                    for term in triple:
                        analysis.bound_metadata(term)
                    if triple not in analysis.graph:
                        raise InputError(
                            "Shape reference witness is outside the profile"
                        )
                nodes[dependency.shape] = None
                nodes[dependency.dependency] = None
            _reference_inventory(analysis, nodes, profile.dependencies)
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
