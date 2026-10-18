"""Checked exact deltas between authorized immutable RDF snapshot scopes."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar, cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.roles.classification import bound_node, bound_text
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import literal_datatype
from bijux_ontology.reports import Issue, Report

from .correspondence import Operation, blank_tokens, compare_witnesses
from .types import (
    BlankNodePair,
    DatasetPatch,
    PatchBlank,
    PatchLiteral,
    PatchPlan,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    PatchValue,
    SnapshotKind,
    SnapshotWitness,
)

_T = TypeVar("_T")


class Rejected(InputError):
    """An established patch precondition or required postcondition failed."""

    def __init__(self, code: str, message: str) -> None:
        """Keep the stable operation diagnostic beside its bounded explanation."""
        super().__init__(message)
        self.code = code


@dataclass
class Captured:
    """One exact witness and its operation-local original blank ownership table."""

    witness: SnapshotWitness
    owned: dict[int, tuple[BNode, PatchBlank]]

    def reference(self, node: BNode) -> PatchBlank:
        """Only actual term objects from this snapshot establish declared binding."""
        entry = self.owned.get(id(node))
        if entry is None or entry[0] is not node:
            raise PatchUnavailable(
                "A blank binding does not belong to its owned snapshot"
            )
        return entry[1]


def capture_items(
    values: Sequence[_T], expected: type[_T], budget: Budget
) -> tuple[_T, ...]:
    """Read one cached finite sequence length before bounded indexed capture."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("RDF patch bindings require finite typed sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError(
                "RDF patch binding inventory changed during capture"
            ) from error
        if not isinstance(value, expected):
            raise InputError("RDF patch binding inventory contains an invalid record")
        result.append(value)
    return tuple(result)


def capture_source(source: PreparedSource, prefix: str, budget: Budget) -> Captured:
    """Read only the actual authorized contexts, retaining empty named graphs."""
    if not isinstance(source, PreparedSource):
        raise InputError("RDF patches require owned PreparedSource inputs")
    if source.union:
        raise PatchUnavailable(
            "A union projection does not establish physical dataset graph inventory"
        )
    if (
        source.literal_fidelity not in ("source_terms", "supplied_terms")
        or source.graph_fidelity != "complete"
    ):
        raise PatchUnavailable(
            "RDF patches require exact literal terms and complete graph inventory"
        )
    budget.consume("resources")
    owned: dict[int, tuple[BNode, PatchBlank]] = {}
    blanks: dict[BNode, PatchBlank] = {}

    def term(node: Node) -> PatchValue:
        """Own exact fields; source blank labels only index this local capture."""
        budget.consume("work")
        bound_node(node, budget)
        if isinstance(node, BNode):
            if node not in blanks:
                budget.consume("states")
                budget.consume("bytes", 64)
                blanks[node] = PatchBlank(prefix + str(len(blanks)))
            if id(node) not in owned:
                budget.consume("states")
                owned[id(node)] = (node, blanks[node])
            return blanks[node]
        if isinstance(node, Literal):
            return PatchLiteral(str(node), str(literal_datatype(node)), node.language)
        if isinstance(node, URIRef):
            return URIRef(node)
        raise InputError("Unsupported RDF patch term")

    kind: SnapshotKind = "graph" if not source.is_dataset else "dataset"
    selection: PatchResource | None = None
    contexts: list[tuple[URIRef | BNode | None, Graph]] = []
    if not source.is_dataset:
        contexts.append((None, source._read(budget=budget)))
    elif source.graph_name is not None:
        if source.graph_name == "@default":
            kind = "default"
            contexts.append((None, source._read(budget=budget)))
        else:
            name = source.graph_name
            bound_text(name, budget)
            if isinstance(name, BNode):
                if not any(
                    value is name for value in source._graph_names(budget=budget)
                ):
                    raise PatchUnavailable(
                        "Pinned blank graph selection lacks source ownership"
                    )
            else:
                name = URIRef(name)
            selection = cast(PatchResource, term(name))
            kind = "named"
            contexts.append((name, source._read(budget=budget)))
    else:
        contexts.append((None, source._read("@default", budget=budget)))
        for name in source._graph_names(budget=budget):
            budget.consume("resources")
            budget.consume("states")
            bound_node(name, budget)
            contexts.append((name, source._read(name, budget=budget)))
    quads = []
    seen_quads: set[PatchQuad] = set()
    graphs = []
    for context_name, graph in contexts:
        budget.consume("work")
        graph_ref = (
            cast(PatchResource, term(context_name))
            if context_name is not None
            else None
        )
        if graph_ref is not None:
            budget.consume("states")
            graphs.append(graph_ref)
        for subject, predicate, value in graph:
            budget.consume("triples")
            budget.consume("work")
            budget.consume("states")
            quad = PatchQuad(
                cast(PatchResource, term(subject)),
                cast(URIRef, term(predicate)),
                term(value),
                graph_ref,
            )
            if quad not in seen_quads:
                budget.consume("states")
                seen_quads.add(quad)
                quads.append(quad)
    witness = SnapshotWitness(kind, selection, tuple(quads), tuple(graphs))
    return Captured(witness, owned)


def replace_witness(
    witness: SnapshotWitness, mapping: dict[PatchBlank, PatchBlank], budget: Budget
) -> SnapshotWitness:
    """Rename only explicit local references, preserving joint graph/quad sharing."""

    def replace(term: PatchValue | None) -> PatchValue | None:
        """Keep nonblank terms exact while applying the declared correspondence."""
        if isinstance(term, PatchBlank):
            return mapping.get(term, term)
        return term

    budget.consume("work", len(witness.quads) + len(witness.graphs))
    budget.consume("states", len(witness.quads) + len(witness.graphs))
    quads = tuple(
        PatchQuad(
            cast(PatchResource, replace(q.subject)),
            q.predicate,
            cast(PatchValue, replace(q.value)),
            cast(PatchResource | None, replace(q.graph)),
        )
        for q in witness.quads
    )
    graphs = tuple(cast(PatchResource, replace(g)) for g in witness.graphs)
    return SnapshotWitness(
        witness.kind,
        cast(PatchResource | None, replace(witness.graph_selection)),
        quads,
        graphs,
    )


def anonymous(witness: SnapshotWitness, budget: Budget) -> SnapshotWitness:
    """Select anonymous assertions jointly, leaving ground-only edits independent."""
    budget.consume("work", len(witness.quads) + len(witness.graphs))
    budget.consume("states", len(witness.quads) + len(witness.graphs))
    quads = tuple(
        q
        for q in witness.quads
        if any(isinstance(t, PatchBlank) for t in (q.subject, q.value, q.graph))
    )
    graphs: set[PatchResource] = {
        g for g in witness.graphs if isinstance(g, PatchBlank)
    }
    graphs.update(q.graph for q in quads if q.graph is not None)
    if witness.graph_selection is not None:
        graphs.add(witness.graph_selection)
    return SnapshotWitness(witness.kind, witness.graph_selection, quads, tuple(graphs))


def edited(
    base: SnapshotWitness,
    removals: tuple[PatchQuad, ...],
    additions: tuple[PatchQuad, ...],
    removed_graphs: tuple[PatchResource, ...],
    added_graphs: tuple[PatchResource, ...],
    budget: Budget,
) -> SnapshotWitness:
    """Apply validated exact record edits only to operation-local owned sets."""
    budget.consume("states", len(base.quads) + len(base.graphs))
    budget.consume("work", len(base.quads) + len(base.graphs))
    quads, graphs = set(base.quads), set(base.graphs)
    for quad in removals:
        budget.consume("work")
        if quad not in quads:
            raise Rejected(
                "rdf.patch_removal_missing",
                "A required quad removal is absent from the accepted base",
            )
        quads.remove(quad)
    for graph in removed_graphs:
        budget.consume("work", len(quads) + 1)
        if graph not in graphs or any(q.graph == graph for q in quads):
            raise Rejected(
                "rdf.patch_removal_missing",
                "A graph removal is absent or would silently remove remaining "
                "assertions",
            )
        graphs.remove(graph)
    for graph in added_graphs:
        budget.consume("work")
        if graph in graphs:
            raise Rejected(
                "rdf.patch_precondition_failed",
                "An added graph already exists in the accepted base",
            )
        budget.consume("states")
        graphs.add(graph)
    for quad in additions:
        budget.consume("work")
        if quad in quads or quad.graph is not None and quad.graph not in graphs:
            raise Rejected(
                "rdf.patch_precondition_failed",
                "An addition already exists or lacks an explicit named graph "
                "inventory entry",
            )
        budget.consume("states")
        quads.add(quad)
    return SnapshotWitness(base.kind, base.graph_selection, tuple(quads), tuple(graphs))


def report(scope: str, issues: IssueCollector, *, complete: bool) -> Report:
    """Keep failed and exhausted scope required without publishing a partial patch."""
    return Report(
        scope,
        int(complete),
        tuple(issues),
        requested=1,
        complete=complete,
        regime="asserted-rdf-dataset-patch",
    )


def plan_rdf_patch(
    base: PreparedSource,
    target: PreparedSource,
    *,
    blank_pairs: Sequence[BlankNodePair] = (),
    created_blanks: Sequence[BNode] = (),
    removed_blanks: Sequence[BNode] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> PatchPlan:
    """Plan exact scope edits; changed anonymous identity requires explicit evidence.

    Declared pairs/new/removed nodes use exact owned objects. Unchanged anonymous
    components need no invented historical identity for ground-only edits.
    Canonical IDs and coincident parser labels never establish correspondence.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    patch = None
    try:
        pairs = capture_items(blank_pairs, BlankNodePair, operation)
        created = capture_items(created_blanks, BNode, operation)
        removed = capture_items(removed_blanks, BNode, operation)
        left, right = (
            capture_source(base, "base:", operation),
            capture_source(target, "new:", operation),
        )
        if left.witness.kind != right.witness.kind:
            raise PatchUnavailable(
                "A patch cannot change or widen its authorized snapshot scope"
            )
        mapping: dict[PatchBlank, PatchBlank] = {}
        matched: set[PatchBlank] = set()
        for pair in pairs:
            operation.consume("work")
            a, b = left.reference(pair.before), right.reference(pair.after)
            if b in mapping or a in matched:
                raise InputError("Declared blank correspondence must be one-to-one")
            operation.consume("states", 2)
            mapping[b] = a
            matched.add(a)
        operation.consume(
            "work", len(created) + len(removed) + len(mapping) + len(matched)
        )
        operation.consume("states", len(created) + len(removed))
        created_refs = {right.reference(node) for node in created}
        removed_refs = {left.reference(node) for node in removed}
        if (
            len(created_refs) != len(created)
            or len(removed_refs) != len(removed)
            or created_refs & mapping.keys()
            or removed_refs & matched
        ):
            raise InputError(
                "Blank correspondence classifications must be disjoint and unique"
            )
        equal, _ = compare_witnesses(left.witness, right.witness, operation)
        if equal:
            patch = DatasetPatch(left.witness, right.witness)
        else:
            same_anonymous, _ = compare_witnesses(
                anonymous(left.witness, operation),
                anonymous(right.witness, operation),
                operation,
            )
            before_graphs: tuple[PatchResource, ...]
            after_graphs: tuple[PatchResource, ...]
            if same_anonymous and not (pairs or created or removed):
                before_quads = tuple(
                    q
                    for q in left.witness.quads
                    if not any(
                        isinstance(t, PatchBlank) for t in (q.subject, q.value, q.graph)
                    )
                )
                after_quads = tuple(
                    q
                    for q in right.witness.quads
                    if not any(
                        isinstance(t, PatchBlank) for t in (q.subject, q.value, q.graph)
                    )
                )
                before_graphs = tuple(
                    g for g in left.witness.graphs if not isinstance(g, PatchBlank)
                )
                after_graphs = tuple(
                    g for g in right.witness.graphs if not isinstance(g, PatchBlank)
                )
                final = right.witness
            else:
                before_tokens, after_tokens = (
                    blank_tokens(left.witness, operation),
                    blank_tokens(right.witness, operation),
                )
                if not before_tokens:
                    operation.consume("states", len(after_tokens))
                    operation.consume("work", len(after_tokens))
                    created_refs = {PatchBlank(token) for token in after_tokens}
                if not after_tokens:
                    operation.consume("states", len(before_tokens))
                    operation.consume("work", len(before_tokens))
                    removed_refs = {PatchBlank(token) for token in before_tokens}
                if {ref.token for ref in matched | removed_refs} != before_tokens or {
                    ref.token for ref in set(mapping) | created_refs
                } != after_tokens:
                    raise PatchUnavailable(
                        "Changed anonymous content requires complete declared "
                        "retained/new/removed node correspondence"
                    )
                final = replace_witness(right.witness, mapping, operation)
                before_quads, after_quads = left.witness.quads, final.quads
                before_graphs, after_graphs = left.witness.graphs, final.graphs
            operation.consume(
                "work",
                len(before_quads)
                + len(after_quads)
                + len(before_graphs)
                + len(after_graphs),
            )
            operation.consume(
                "states",
                len(before_quads)
                + len(after_quads)
                + len(before_graphs)
                + len(after_graphs),
            )
            left_q, right_q, left_g, right_g = (
                set(before_quads),
                set(after_quads),
                set(before_graphs),
                set(after_graphs),
            )
            removals = tuple(q for q in before_quads if q not in right_q)
            additions = tuple(q for q in after_quads if q not in left_q)
            removed_graphs = tuple(g for g in before_graphs if g not in right_g)
            added_graphs = tuple(g for g in after_graphs if g not in left_g)
            prospective = edited(
                left.witness,
                removals,
                additions,
                removed_graphs,
                added_graphs,
                operation,
            )
            accepted, _ = compare_witnesses(prospective, final, operation)
            if not accepted:
                raise Rejected(
                    "rdf.patch_postcondition_failed",
                    "Planned edits do not reconstruct the requested target",
                )
            patch = DatasetPatch(
                left.witness, final, removals, additions, removed_graphs, added_graphs
            )
        operation.consume("work", 0)
    except LimitExceeded as error:
        patch = None
        issues.exhausted(str(error), location="rdf.patch")
    except (PatchUnavailable, ResourceError) as error:
        patch = None
        try:
            issues.append(Issue("rdf.patch_unverified", str(error), kind="unverified"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.patch")
    except Rejected as error:
        patch = None
        try:
            issues.append(Issue(error.code, str(error), kind="input_error"))
        except LimitExceeded as limit:
            issues.exhausted(str(limit), location="rdf.patch")
    return PatchPlan(
        patch, report("rdf.patch_planning", issues, complete=patch is not None)
    )
