"""Bounded structural overlap domains over existing exact RDF patch plans."""

import threading
from dataclasses import dataclass
from typing import Literal, cast

from rdflib import BNode, URIRef
from rdflib.namespace import RDF

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.patches.planning import _plan_captured_patch, capture_source
from bijux_ontology.rdf.patches.types import (
    BlankNodePair,
    DatasetPatch,
    PatchBlank,
    PatchBlankBinding,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    SnapshotWitness,
)
from bijux_ontology.rdf.snapshots import PreparedSource

from .types import (
    MergeChanges,
    MergeConflict,
    MergeCorrespondence,
    RdfMergePlan,
    _Component,
)


@dataclass(frozen=True)
class _Atom:
    """One original exact branch operation, used only for local overlap grouping."""

    side: int
    kind: Literal["remove", "add", "remove_graph", "add_graph"]
    value: PatchQuad | PatchResource


def _changes(atoms: list[_Atom], side: int, operation: Budget) -> MergeChanges:
    """Freeze a branch component only after reserving every output reference."""
    operation.consume("work", 4 * len(atoms))
    operation.consume("states", len(atoms) + 1)
    operation.consume("bytes", 64 + 16 * len(atoms))
    return MergeChanges(
        tuple(
            cast(PatchQuad, a.value)
            for a in atoms
            if a.side == side and a.kind == "remove"
        ),
        tuple(
            cast(PatchQuad, a.value)
            for a in atoms
            if a.side == side and a.kind == "add"
        ),
        tuple(
            cast(PatchResource, a.value)
            for a in atoms
            if a.side == side and a.kind == "remove_graph"
        ),
        tuple(
            cast(PatchResource, a.value)
            for a in atoms
            if a.side == side and a.kind == "add_graph"
        ),
    )


def _same(left: MergeChanges, right: MergeChanges, operation: Budget) -> bool:
    """Coalesce equal operations independently of graph traversal order."""
    for a, b in zip(
        (left.removals, left.additions, left.removed_graphs, left.added_graphs),
        (right.removals, right.additions, right.removed_graphs, right.added_graphs),
        strict=True,
    ):
        operation.consume("work", len(a) + len(b))
        operation.consume("states", len(a) + len(b))
        if set(a) != set(b):
            return False
    return True


def _list_domains(
    witnesses: tuple[SnapshotWitness, ...], operation: Budget
) -> dict[tuple[PatchResource | None, PatchResource], int]:
    """Group connected list-cell assertions without interpreting list sequences.

    This is only a conservative edit-overlap domain. Rest-linked cells, nested
    cells and shared tails join even when different positions are edited. It
    makes no claim that a list is valid, finite or semantically ordered; actual
    selected application checks retain that responsibility.
    """
    cells: dict[tuple[PatchResource | None, PatchResource], set[PatchResource]] = {}
    for witness in witnesses:
        for q in witness.quads:
            operation.consume("work")
            if q.predicate in (RDF.first, RDF.rest):
                key = (q.graph, q.subject)
                if key not in cells:
                    operation.consume("states")
                    operation.consume("bytes", 96)
                    cells[key] = set()
    for witness in witnesses:
        for q in witness.quads:
            operation.consume("work")
            if (
                q.predicate not in (RDF.first, RDF.rest)
                or not isinstance(q.value, URIRef | PatchBlank)
                or q.value == RDF.nil
            ):
                continue
            key, other = (q.graph, q.subject), (q.graph, q.value)
            if key in cells and other in cells:
                operation.consume("states", 2)
                cells[key].add(q.value)
                cells[other].add(q.subject)
    domains: dict[tuple[PatchResource | None, PatchResource], int] = {}
    for key in cells:
        operation.consume("work")
        if key in domains:
            continue
        number = len(domains)
        operation.consume("states")
        pending = [key]
        while pending:
            operation.consume("work")
            current = pending.pop()
            if current in domains:
                continue
            operation.consume("states")
            domains[current] = number
            for adjacent in cells[current]:
                operation.consume("work")
                operation.consume("states")
                pending.append((current[0], adjacent))
    return domains


def _components(
    base: SnapshotWitness,
    left: DatasetPatch,
    right: DatasetPatch,
    conflicts: list[MergeConflict],
    parts: list[_Component],
    issues: IssueCollector,
    operation: Budget,
) -> None:
    """Partition all edits, then retain each complete conflict before further work."""
    atoms: list[_Atom] = []
    inventory: set[PatchResource] = set()
    for side, patch in enumerate((left, right)):
        for kind, values in (
            ("remove", patch.removals),
            ("add", patch.additions),
            ("remove_graph", patch.removed_graphs),
            ("add_graph", patch.added_graphs),
        ):
            for value in values:
                operation.consume("work")
                operation.consume("states", 2)
                operation.consume("bytes", 64)
                atoms.append(
                    _Atom(
                        side,
                        cast(
                            Literal["remove", "add", "remove_graph", "add_graph"], kind
                        ),
                        value,
                    )
                )
                if kind in ("remove_graph", "add_graph"):
                    inventory.add(cast(PatchResource, value))
    lists = _list_domains((base, left.target, right.target), operation)
    operation.consume("states", len(atoms))
    operation.consume("bytes", 16 * len(atoms))
    parents = list(range(len(atoms)))
    domains: dict[tuple[object, ...], int] = {}
    kinds: dict[int, set[str]] = {}

    def root(index: int) -> int:
        """Find an operation-local edit component with charged path compression."""
        while parents[index] != index:
            operation.consume("work")
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, atom in enumerate(atoms):
        operation.consume("work")
        keys: list[tuple[object, ...]] = []
        if isinstance(atom.value, PatchQuad):
            q = atom.value
            keys.append(("property", q.graph, q.subject, q.predicate))
            if q.graph in inventory:
                keys.append(("graph", q.graph))
            for term in (q.subject, q.value):
                operation.consume("work")
                number = lists.get(
                    cast(tuple[PatchResource | None, PatchResource], (q.graph, term))
                )
                if number is not None:
                    keys.append(("list", q.graph, number))
        else:
            keys.append(("graph", atom.value))
        for key in keys:
            operation.consume("work")
            if key in domains:
                first_root, second_root = root(index), root(domains[key])
                parents[first_root] = second_root
            else:
                operation.consume("states")
                operation.consume("bytes", 64)
                domains[key] = index
    grouped: dict[int, list[_Atom]] = {}
    for index, atom in enumerate(atoms):
        operation.consume("work")
        group_number = root(index)
        operation.consume("states", 2)
        operation.consume("bytes", 32)
        grouped.setdefault(group_number, []).append(atom)
    for key, index in domains.items():
        operation.consume("work")
        operation.consume("states")
        kinds.setdefault(root(index), set()).add(cast(str, key[0]))
    for group_number, group in grouped.items():
        a, b = _changes(group, 0, operation), _changes(group, 1, operation)
        operation.consume("work", 2 * len(group))
        overlap = any(atom.side == 0 for atom in group) and any(
            atom.side == 1 for atom in group
        )
        conflict = None
        operation.consume("states", 2)
        operation.consume("bytes", 128)
        if overlap and not _same(a, b, operation):
            conflict = len(conflicts)
            kind = (
                "graph"
                if "graph" in kinds[group_number]
                else "list"
                if "list" in kinds[group_number]
                else "property"
            )
            # Freeze proved evidence before a possibly exhausted issue reservation.
            conflicts.append(
                MergeConflict(
                    conflict, cast(Literal["property", "list", "graph"], kind), a, b
                )
            )
            parts.append(_Component(a, b, conflict))
            issues.append(
                Issue(
                    "rdf.merge_conflict",
                    "Branches changed an overlapping " + kind + " domain",
                    location=str(conflict),
                    kind="finding",
                )
            )
        else:
            parts.append(_Component(a, b, None))


def plan_rdf_merge(
    base: PreparedSource,
    left: PreparedSource,
    right: PreparedSource,
    *,
    left_correspondence: MergeCorrespondence | None = None,
    right_correspondence: MergeCorrespondence | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: threading.Event | None = None,
) -> RdfMergePlan:
    """Compare exact branches over one actual base, without choosing conflicts.

    Changed anonymous content uses existing explicit owned correspondence.
    Cosmetic relabels and ground-only edits do not demand unused blank identity.
    Conflicts are structural overlaps, not OWL inconsistency or field semantics.
    An incomplete plan never permits a merge candidate.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    conflicts: list[MergeConflict] = []
    parts: list[_Component] = []
    bindings: list[PatchBlankBinding] = []
    witness = None
    ready = False
    checked = 0
    try:
        captured = capture_source(base, "base:", operation)
        witness = captured.witness
        seen: set[str] = set()
        for node, ref in captured.owned.values():
            operation.consume("work")
            if ref.token not in seen:
                operation.consume("states", 2)
                operation.consume("bytes", 32)
                seen.add(ref.token)
                bindings.append(PatchBlankBinding(ref.token, node))
        patches: list[DatasetPatch] = []
        for source, declared, prefix in (
            (left, left_correspondence, "left:"),
            (right, right_correspondence, "right:"),
        ):
            declared = MergeCorrespondence() if declared is None else declared
            if not isinstance(declared, MergeCorrespondence):
                raise InputError("Merge correspondence requires explicit typed records")
            target = capture_source(source, prefix, operation)
            pairs = capture_items(declared.pairs, BlankNodePair, operation)
            created = capture_items(declared.created, BNode, operation)
            removed = capture_items(declared.removed, BNode, operation)
            operation.consume("states")
            if captured.witness.kind == "named" and (
                isinstance(captured.witness.graph_selection, URIRef)
                and captured.witness.graph_selection != target.witness.graph_selection
            ):
                raise PatchUnavailable(
                    "Branches cannot change the authorized named graph selection"
                )
            patches.append(
                _plan_captured_patch(
                    captured,
                    target,
                    pairs=pairs,
                    created=created,
                    removed=removed,
                    operation=operation,
                )
            )
            checked += 1
        _components(
            witness, patches[0], patches[1], conflicts, parts, issues, operation
        )
        operation.consume("work", 0)
        ready = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="rdf.merge")
    except (PatchUnavailable, ResourceError) as error:
        try:
            issues.append(Issue("rdf.merge_unverified", str(error), kind="unverified"))
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted), location="rdf.merge")
    report = Report(
        "rdf.merge_planning",
        checked,
        tuple(issues),
        requested=2,
        complete=ready,
        regime="exact-common-base-structural-merge",
    )
    return RdfMergePlan._from_planning(
        witness, tuple(conflicts), report, tuple(parts), base, tuple(bindings), ready
    )
