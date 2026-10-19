"""Application topology over one explicitly selected, owned RDF corpus."""

import json
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Graph, Literal, URIRef
from rdflib.term import Node

from bijux_ontology.declarations.topology import TopologyContract, TopologyRelation
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.json.paths import bound_index, bounded_location
from bijux_ontology.rdf.path_traversal import (
    PathIdentityUnavailable,
    finite_path_steps,
    path_values,
)
from bijux_ontology.rdf.paths import capture_path
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.terms import bound_node, term_key

from .types import TopologyRecord


@dataclass(frozen=True)
class _Focus:
    """One qualified record binding and bounded caller diagnostic anchor."""

    record: TopologyRecord
    where: str


@dataclass(frozen=True)
class _Link:
    """An established selected path, never a fabricated asserted RDF triple."""

    source: _Focus
    target: _Focus | None
    endpoint: Node
    relation: TopologyRelation
    path: str


def _capture(
    values: object, expected: type[object], budget: Budget, *, count: int | None = None
) -> tuple[object, ...]:
    """Own a bounded finite input inventory before semantic work begins."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Topology inventories require finite typed sequences")
    count = len(values) if count is None else count
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            value = values[index]
        except IndexError as error:
            raise InputError("A topology inventory changed during capture") from error
        if not isinstance(value, expected):
            raise InputError("A topology inventory contains an unsupported item")
        if expected is str:
            bound_text(cast(str, value), budget)
            value = str(value)
        result.append(value)
    return tuple(result)


def _place(record: TopologyRecord, budget: Budget) -> str:
    """Bound known output metadata without inventing a source occurrence."""
    bound_text(record.identifier, budget)
    bound_node(record.subject, budget)
    location = record.location
    if location is None:
        return record.identifier
    for value in (location.source, location.term, location.graph, location.model):
        if value is not None:
            bound_text(value, budget)
    if location.branch is not None:
        bound_index(location.branch, budget)
    pointer = bounded_location(location.path, budget)
    size = len(location.source) + len(pointer)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Topology output location exceeds max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", 4 * size)
    if (
        len(location.source.encode("utf-8")) + len(pointer.encode("utf-8"))
        > budget.limits.max_bytes
    ):
        raise LimitExceeded("Topology output location exceeds max_bytes")
    return (
        location.source + pointer
        or location.model
        or location.term
        or location.graph
        or record.identifier
    )


def _presence(
    graph: Graph, subject: URIRef | BNode, budget: Budget
) -> tuple[bool, bool]:
    """Check actual graph presence and strict blank identity without a new index."""
    if isinstance(subject, BNode):
        for triple in graph:
            budget.consume("work")
            if triple[0] is subject or triple[2] is subject:
                return True, True
        return False, False
    for pattern in ((subject, None, None), (None, None, subject)):
        for _ in graph.triples(pattern):
            budget.consume("work")
            return True, True
    return False, True


def _context(
    source: PreparedSource, name: GraphSelection | None, union: bool, budget: Budget
) -> tuple[dict[str, object], int]:
    """Retain exact operation scope, not guessed graph names or source paths."""
    if name is not None:
        bound_text(name, budget)
    if source.graph_name is not None:
        bound_text(source.graph_name, budget)
    name = source.graph_name if source.graph_name is not None else name
    union = source.union or union
    for value in (
        source.document_iri,
        source.effective_url,
        source.literal_fidelity,
        str(name) if name is not None else None,
    ):
        if value is not None:
            bound_text(value, budget)
    kind = (
        "union"
        if union
        else "named"
        if name is not None and name != "@default"
        else "default"
        if source.is_dataset
        else "graph"
    )
    graph = (
        term_key(name if isinstance(name, URIRef | BNode) else URIRef(name))
        if kind == "named" and name is not None
        else None
    )
    result: dict[str, object] = {
        "document_iri": source.document_iri,
        "effective_url": source.effective_url,
        "graph_kind": kind,
        "graph_name": graph,
    }
    size = 128 + 12 * (
        len(source.document_iri or "")
        + len(source.effective_url or "")
        + len(str(name or ""))
    )
    budget.consume("work", size)
    budget.consume("bytes", size)
    return result, size


def _evidence(
    context: dict[str, object],
    context_size: int,
    focuses: Sequence[_Focus],
    links: Sequence[_Link],
    budget: Budget,
) -> str:
    """Reserve all derived evidence before materializing nested JSON records."""
    budget.consume("states", 8 * (1 + len(focuses) + len(links)))
    selected: dict[str, _Focus] = {}
    for focus in focuses:
        selected[focus.record.identifier] = focus
    for link in links:
        selected[link.source.record.identifier] = link.source
        if link.target is not None:
            selected[link.target.record.identifier] = link.target
    size = 512 + context_size
    for focus in selected.values():
        size += 256 + 12 * (
            len(focus.record.identifier)
            + len(focus.where)
            + sum(len(part or "") for part in term_key(focus.record.subject))
        )
    for link in links:
        size += 256 + 12 * (
            len(link.source.record.identifier)
            + len(link.target.record.identifier if link.target else "")
            + len(link.relation.name)
            + len(link.path)
            + sum(len(part or "") for part in term_key(link.endpoint))
        )
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Topology evidence exceeds max_bytes")
    budget.consume("work", size)
    budget.consume("bytes", size)
    data = {
        "source": context,
        "records": [
            {
                "identifier": f.record.identifier,
                "subject": term_key(f.record.subject),
                "output_location": f.where if f.record.location is not None else None,
            }
            for f in selected.values()
        ],
        "paths": [
            {
                "source": link.source.record.identifier,
                "target": link.target.record.identifier if link.target else None,
                "endpoint": term_key(link.endpoint),
                "relation": link.relation.name,
                "path": link.path,
            }
            for link in links
        ],
    }
    text = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if len(text) > budget.limits.max_bytes:
        raise LimitExceeded("Topology evidence exceeds max_bytes")
    return text


def _path(
    adjacency: dict[str, dict[str, _Link]], start: str, goal: str, budget: Budget
) -> tuple[_Link, ...] | None:
    """Find an already established path for one incremental cycle witness."""
    if start == goal:
        return ()
    queue = deque([start])
    parents: dict[str, _Link] = {}
    seen = {start}
    while queue:
        node = queue.popleft()
        budget.consume("work")
        for target, link in adjacency.get(node, {}).items():
            budget.consume("work")
            if target in seen:
                continue
            budget.consume("states", 2)
            seen.add(target)
            parents[target] = link
            if target == goal:
                result = []
                current = goal
                while current != start:
                    budget.consume("work")
                    budget.consume("states")
                    item = parents[current]
                    result.append(item)
                    current = item.source.record.identifier
                return tuple(reversed(result))
            queue.append(target)
    return None


def check_record_topology(
    source: PreparedSource,
    records: Sequence[TopologyRecord],
    contract: TopologyContract,
    *,
    roots: Sequence[str],
    shared_targets: Sequence[str] = (),
    complete: bool,
    closure: Choice["open", "closed"],
    graph_name: GraphSelection | None = None,
    union: bool = False,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Check selected record paths as explicit application constraints.

    Complete identifies the required supplied inventory; closed additionally
    permits absence claims within that boundary. Open/partial scope keeps
    missing references, reachability and minimum ownership unresolved. Known
    cycles and excess owners remain findings. Source selection applies equally
    to all traversed support: hidden named graphs never fill a missing path.
    """
    if not isinstance(source, PreparedSource) or not isinstance(
        contract, TopologyContract
    ):
        raise InputError("Expected owned RDF source and typed TopologyContract")
    if (
        type(complete) is not bool
        or type(union) is not bool
        or closure not in ("open", "closed")
    ):
        raise InputError("Declare complete inventory, explicit closure and graph union")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Topology limits conflict with the shared Budget")
    if graph_name is not None and not isinstance(graph_name, str | URIRef | BNode):
        raise InputError("Topology graph selection requires a typed RDF name")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    checked = 0
    finished = True
    requested = (
        len(records)
        if isinstance(records, Sequence)
        and not isinstance(cast(object, records), str | bytes)
        else 0
    )
    try:
        captured = cast(
            tuple[TopologyRecord, ...],
            _capture(records, TopologyRecord, operation, count=requested),
        )
        root_ids = cast(tuple[str, ...], _capture(roots, str, operation))
        shared_ids = cast(tuple[str, ...], _capture(shared_targets, str, operation))
        if len(set(root_ids)) != len(root_ids) or len(set(shared_ids)) != len(
            shared_ids
        ):
            raise InputError("Topology roots and shared targets must be unique")
        for value in (contract.min_owners, contract.max_owners):
            if value is not None:
                bound_index(value, operation)
        scope, scope_size = _context(source, graph_name, union, operation)
        selected_name = (
            source.graph_name if source.graph_name is not None else graph_name
        )
        if isinstance(selected_name, BNode) and not any(
            name is selected_name for name in source._graph_names(budget=operation)
        ):
            issues.append(
                Issue(
                    "data.topology_scope_unverified",
                    "Blank graph selection lacks exact source ownership",
                    kind="unverified",
                )
            )
            return Report(
                "record-topology",
                0,
                tuple(issues),
                requested=requested,
                complete=False,
                regime="application-topology",
            )
        graph = source._read(graph_name, union=union, budget=operation)
        focuses: dict[str, _Focus] = {}
        nodes: dict[tuple[str | None, ...], _Focus] = {}
        bindings: set[object] = set()
        unknown_inventory = False
        for record in captured:
            where = _place(record, operation)
            if record.identifier in focuses:
                raise InputError("Topology record identifiers must be unique")
            operation.consume("states")
            focus = _Focus(record, where)
            focuses[record.identifier] = focus
            binding = (
                ("iri", str(record.subject))
                if isinstance(record.subject, URIRef)
                else ("blank", id(record.subject))
            )
            if binding in bindings:
                raise InputError("Topology record subjects must be unique")
            operation.consume("states")
            bindings.add(binding)
            present, owned = _presence(graph, record.subject, operation)
            if not owned:
                unknown_inventory = True
                checked += 1
                issues.append(
                    Issue(
                        "data.topology_binding_unverified",
                        "Blank record binding lacks exact snapshot ownership",
                        where,
                        kind="unverified",
                        details=record.location,
                    )
                )
                continue
            if not present:
                unknown = not (complete and closure == "closed")
                unknown_inventory = unknown_inventory or unknown
                checked += 1
                issues.append(
                    Issue(
                        "data.topology_record_missing",
                        "Record has no established occurrence in the selected graph",
                        where,
                        kind="unverified" if unknown else "finding",
                        details=record.location,
                    )
                )
                continue
            key = term_key(record.subject)
            if key in nodes:
                raise InputError("Topology record subjects must be unique")
            operation.consume("states")
            nodes[key] = focus
        if set(root_ids) - focuses.keys() or set(shared_ids) - focuses.keys():
            raise InputError("Roots and shared targets must identify supplied records")
        root_set, shared_set = set(root_ids), set(shared_ids)
        closed = complete and closure == "closed" and not unknown_inventory
        reach_unknown = owner_unknown = unknown_inventory
        adjacency: dict[str, dict[str, _Link]] = {}
        reachable_edges: dict[str, set[str]] = {}
        owners: dict[str, dict[str, _Link]] = {}
        cycles: set[frozenset[str]] = set()
        excess: set[str] = set()
        relations = []
        for relation in contract.relations:
            bound_text(relation.name, operation)
            path = capture_path(relation.path, budget=operation)
            steps = finite_path_steps(path, operation)
            readable = path.readable
            bound_text(readable, operation)
            relations.append((relation, steps, readable))
            if steps is None:
                issues.append(
                    Issue(
                        "data.topology_path_unverified",
                        "Topology requires finite sequence/inverse paths",
                        relation.name,
                        kind="unverified",
                    )
                )
                reach_unknown = reach_unknown or relation.reachability
                owner_unknown = owner_unknown or relation.ownership
        for focus in nodes.values():
            source_id = focus.record.identifier
            for relation, steps, readable in relations:
                if steps is None:
                    continue
                try:
                    endpoints = path_values(
                        graph,
                        focus.record.subject,
                        steps,
                        operation,
                        literal_identity=source.literal_fidelity
                        in {"source_terms", "supplied_terms"},
                    )
                except PathIdentityUnavailable as error:
                    issues.append(
                        Issue(
                            "data.topology_path_unverified",
                            str(error),
                            focus.where,
                            kind="unverified",
                            expected=readable,
                            details=focus.record.location,
                        )
                    )
                    reach_unknown = reach_unknown or relation.reachability
                    owner_unknown = owner_unknown or relation.ownership
                    continue
                for endpoint in endpoints:
                    operation.consume("work")
                    target = nodes.get(term_key(endpoint))
                    link = _Link(focus, target, endpoint, relation, readable)
                    if isinstance(endpoint, Literal):
                        issues.append(
                            Issue(
                                "data.topology_target_invalid",
                                "A selected record relation ends in a literal",
                                focus.where,
                                details=focus.record.location,
                                evidence_json=_evidence(
                                    scope, scope_size, (), (link,), operation
                                ),
                            )
                        )
                        continue
                    if target is None:
                        issues.append(
                            Issue(
                                "data.topology_reference_missing"
                                if closed
                                else "data.topology_reference_unverified",
                                "Relation target is outside the declared record corpus",
                                focus.where,
                                kind="finding" if closed else "unverified",
                                details=focus.record.location,
                                evidence_json=_evidence(
                                    scope, scope_size, (), (link,), operation
                                ),
                            )
                        )
                        reach_unknown = reach_unknown or relation.reachability
                        owner_unknown = owner_unknown or relation.ownership
                        continue
                    target_id = target.record.identifier
                    if relation.reachability:
                        operation.consume("states")
                        reachable_edges.setdefault(source_id, set()).add(target_id)
                    if relation.ownership:
                        incoming = owners.setdefault(target_id, {})
                        if source_id not in incoming:
                            operation.consume("states")
                            incoming[source_id] = link
                        maximum = contract.max_owners
                        if (
                            maximum is not None
                            and len(incoming) > maximum
                            and target_id not in shared_set
                            and target_id not in excess
                        ):
                            operation.consume("states", 1 + len(incoming))
                            proof = tuple(incoming.values())
                            issues.append(
                                Issue(
                                    "data.topology_owners",
                                    "More distinct owners than the declared maximum",
                                    target.where,
                                    expected=str(maximum),
                                    actual=str(len(incoming)),
                                    details=target.record.location,
                                    evidence_json=_evidence(
                                        scope, scope_size, (), proof, operation
                                    ),
                                )
                            )
                            excess.add(target_id)
                    if relation.acyclic and target_id not in adjacency.get(
                        source_id, {}
                    ):
                        cycle_path = _path(adjacency, target_id, source_id, operation)
                        if cycle_path is not None:
                            operation.consume("states", 1 + len(cycle_path))
                            identity = frozenset(
                                (
                                    source_id,
                                    target_id,
                                    *(
                                        item.source.record.identifier
                                        for item in cycle_path
                                    ),
                                )
                            )
                            if identity not in cycles:
                                issues.append(
                                    Issue(
                                        "data.topology_cycle",
                                        "Selected application paths form a cycle",
                                        focus.where,
                                        details=focus.record.location,
                                        evidence_json=_evidence(
                                            scope,
                                            scope_size,
                                            (),
                                            (link, *cycle_path),
                                            operation,
                                        ),
                                    )
                                )
                                cycles.add(identity)
                        operation.consume("states")
                        adjacency.setdefault(source_id, {})[target_id] = link
            checked += 1
        valid_ids = {focus.record.identifier for focus in nodes.values()}
        reached = root_set & valid_ids
        operation.consume("states", len(reached))
        queue = deque(reached)
        while queue:
            current = queue.popleft()
            operation.consume("work")
            for target_id in reachable_edges.get(current, ()):
                operation.consume("work")
                if target_id not in reached:
                    operation.consume("states")
                    reached.add(target_id)
                    queue.append(target_id)
        if contract.require_reachable and not root_ids:
            issues.append(
                Issue(
                    "data.topology_roots_unverified",
                    "No required root record binding was supplied",
                    kind="unverified",
                )
            )
        for focus in nodes.values():
            operation.consume("work")
            identifier = focus.record.identifier
            if contract.require_reachable and identifier not in reached:
                known = closed and bool(root_ids) and not reach_unknown
                issues.append(
                    Issue(
                        "data.topology_unreachable",
                        "Record is not established reachable from the selected roots",
                        focus.where,
                        kind="finding" if known else "unverified",
                        details=focus.record.location,
                        evidence_json=_evidence(
                            scope, scope_size, (focus,), (), operation
                        ),
                    )
                )
            minimum = contract.min_owners
            count = len(owners.get(identifier, {}))
            if minimum is not None and identifier not in root_set and count < minimum:
                known = closed and not owner_unknown
                issues.append(
                    Issue(
                        "data.topology_owners",
                        "Required incoming ownership is not established",
                        focus.where,
                        kind="finding" if known else "unverified",
                        expected=str(minimum),
                        actual=str(count),
                        details=focus.record.location,
                        evidence_json=_evidence(
                            scope, scope_size, (focus,), (), operation
                        ),
                    )
                )
        if not complete:
            issues.append(
                Issue(
                    "data.topology_corpus_unverified",
                    "Required record inventory is not declared complete",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location="record-topology")
    return Report(
        "record-topology",
        checked,
        tuple(issues),
        requested=requested,
        complete=finished,
        regime="application-topology",
        families=("application-topology",),
    )
