"""Bounded intersection, difference and symmetric difference of owned records."""

from collections.abc import Sequence
from typing import Literal

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.correspondence import blank_tokens
from bijux_ontology.rdf.patches.planning import (
    Captured,
    capture_source,
    replace_witness,
)
from bijux_ontology.rdf.patches.types import (
    BlankNodePair,
    PatchBlank,
    PatchUnavailable,
    SnapshotWitness,
)
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.transforms import Transformation
from bijux_ontology.reports import Issue, Report


def _named(witness: SnapshotWitness, operation: Budget) -> SnapshotWitness:
    """Select named-only records explicitly; anonymous graph records are excluded."""
    operation.consume("work", len(witness.quads) + len(witness.graphs))
    operation.consume("states", len(witness.quads) + len(witness.graphs))
    return SnapshotWitness(
        witness.kind,
        witness.graph_selection,
        tuple(
            quad
            for quad in witness.quads
            if not any(
                isinstance(term, PatchBlank)
                for term in (quad.subject, quad.value, quad.graph)
            )
        ),
        tuple(graph for graph in witness.graphs if not isinstance(graph, PatchBlank)),
    )


def _correspondence(
    left: Captured,
    right: Captured,
    pairs: tuple[BlankNodePair, ...],
    same_owner: bool,
    operation: Budget,
) -> SnapshotWitness:
    """Align only one snapshot's identity or complete declared owned pairs."""
    mapping: dict[PatchBlank, PatchBlank] = {}
    used: set[PatchBlank] = set()
    operation.consume("states", len(left.owned) + len(right.owned) + 2 * len(pairs))
    operation.consume("work", len(left.owned) + len(right.owned))
    if same_owner:
        if pairs:
            raise InputError("One owned snapshot already establishes blank identity")
        owned = dict(left.owned.values())
        for node, reference in right.owned.values():
            if node in owned:
                mapping[reference] = owned[node]
    else:
        for pair in pairs:
            operation.consume("work")
            before, after = left.reference(pair.before), right.reference(pair.after)
            if before in used or after in mapping:
                raise InputError("Set algebra correspondence must be one-to-one")
            mapping[after] = before
            used.add(before)
        left_tokens = blank_tokens(left.witness, operation)
        right_tokens = blank_tokens(right.witness, operation)
        if {ref.token for ref in used} != left_tokens or {
            ref.token for ref in mapping
        } != right_tokens:
            raise PatchUnavailable(
                "Independent anonymous inputs require complete owned correspondence "
                "or an explicit named-only scope"
            )
    return replace_witness(right.witness, mapping, operation)


def rdf_set_operation(
    left: PreparedSource,
    right: PreparedSource,
    operation: Literal["intersection", "difference", "symmetric_difference"],
    *,
    left_graph: GraphSelection | None = None,
    right_graph: GraphSelection | None = None,
    blank_pairs: Sequence[BlankNodePair] = (),
    named_only: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Transformation:
    """Compute exact asserted records, never inferred or isomorphic equality.

    Explicit/pinned graph selections are projected to triples and return a graph.
    Unselected datasets operate on quads and graph inventories. Inventory follows
    the selected set operation, with every retained quad's graph also retained;
    consequently difference may retain a common graph that still has left-only
    quads. Empty graphs participate independently. Both inputs must have the same
    graph/dataset scope. Unions and incomplete identity fidelity remain unknown.

    One PreparedSource establishes shared anonymous identity across its selected
    graphs. Independent sources require complete one-to-one BlankNodePair
    correspondence or named_only=True, which excludes every record containing
    a blank subject, object or graph. Inputs are never changed. Result blanks
    belong to a fresh snapshot while retaining their joint sharing.
    """
    if operation not in ("intersection", "difference", "symmetric_difference"):
        raise InputError("Choose intersection, difference or symmetric_difference")
    if type(named_only) is not bool:
        raise InputError("named_only must be a boolean")
    if not isinstance(left, PreparedSource) or not isinstance(right, PreparedSource):
        raise InputError("Set algebra requires owned PreparedSource inputs")
    owned = Operation(limits, budget, 30, None)
    issues = IssueCollector(owned)
    candidate = None
    try:
        pairs = capture_items(blank_pairs, BlankNodePair, owned)
        if named_only and pairs:
            raise InputError("Named-only algebra does not accept anonymous bindings")
        first = capture_source(
            left,
            "left:",
            owned,
            graph_name=left_graph,
            project_graph=left_graph is not None or left.graph_name is not None,
        )
        second = capture_source(
            right,
            "right:",
            owned,
            graph_name=right_graph,
            project_graph=right_graph is not None or right.graph_name is not None,
        )
        before, after = first.witness, second.witness
        if before.kind != after.kind:
            raise PatchUnavailable("Set algebra requires matching graph/dataset scopes")
        if named_only:
            before, after = _named(before, owned), _named(after, owned)
        else:
            after = _correspondence(first, second, pairs, left is right, owned)
        size = (
            len(before.quads)
            + len(after.quads)
            + len(before.graphs)
            + len(after.graphs)
        )
        owned.consume("work", size)
        owned.consume("states", 3 * size)
        owned.consume("bytes", 128 + 64 * size)
        left_q, right_q = set(before.quads), set(after.quads)
        left_g, right_g = set(before.graphs), set(after.graphs)
        if operation == "intersection":
            quads, graphs = left_q & right_q, left_g & right_g
        elif operation == "difference":
            quads, graphs = left_q - right_q, left_g - right_g
        else:
            quads, graphs = left_q ^ right_q, left_g ^ right_g
        for quad in quads:
            owned.consume("work")
            if quad.graph is not None:
                graphs.add(quad.graph)
        witness = SnapshotWitness(before.kind, None, tuple(quads), tuple(graphs))
        candidate = _materialize(witness, left, owned)
    except PatchUnavailable as error:
        try:
            issues.append(
                Issue("rdf.algebra_identity_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as limit:
            issues.exhausted(str(limit))
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Transformation(
        candidate,
        Report(
            "rdf.set_algebra",
            int(candidate is not None),
            tuple(issues),
            requested=1,
            complete=candidate is not None,
            regime="asserted-rdf-named-only-set-algebra"
            if named_only
            else "asserted-rdf-set-algebra",
        ),
    )
