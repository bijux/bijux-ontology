"""Application topology policies over qualified corpus and incremental evidence."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal as Choice
from typing import cast

from rdflib import BNode, Literal, URIRef

from bijux_ontology.data.records.reading import read_records
from bijux_ontology.declarations.topology import TopologyContract
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import bound_index
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource

from .capture import RecordCorpus, capture_inventory
from .evidence import RelationEvidence, _context, _Gap, _Link
from .types import TopologyRecord


@dataclass
class _Policy:
    """Application constraint decisions over already established selected facts."""

    corpus: RecordCorpus
    evidence: RelationEvidence
    contract: TopologyContract
    issues: IssueCollector
    cycles: set[frozenset[str]] = field(default_factory=set)
    excess: set[str] = field(default_factory=set)

    def unresolved(self, gap: _Gap) -> None:
        """Expose unresolved path coverage without pretending it is an empty result."""
        focus = gap.focus
        if focus is None:
            self.issues.append(
                Issue(
                    "data.topology_path_unverified",
                    "Topology requires finite sequence/inverse paths",
                    gap.relation.name,
                    kind="unverified",
                )
            )
        else:
            self.issues.append(
                Issue(
                    "data.topology_path_unverified",
                    gap.reason or "Topology requires finite sequence/inverse paths",
                    focus.where,
                    kind="unverified",
                    expected=gap.path,
                    details=focus.record.location,
                )
            )
        self.evidence.unresolved(gap.relation)

    def observe(self, link: _Link) -> None:
        """Retain known violations before requesting any subsequent relation data."""
        focus, target, endpoint, relation = (
            link.source,
            link.target,
            link.endpoint,
            link.relation,
        )
        source_id = focus.record.identifier
        operation, issues, contract = self.issues.budget, self.issues, self.contract
        if isinstance(endpoint, Literal):
            issues.append(
                Issue(
                    "data.topology_target_invalid",
                    "A selected record relation ends in a literal",
                    focus.where,
                    details=focus.record.location,
                    evidence_json=self.evidence.evidence((), (link,), operation),
                )
            )
            return
        if target is None:
            issues.append(
                Issue(
                    "data.topology_reference_missing"
                    if self.corpus.closed
                    else "data.topology_reference_unverified",
                    "Relation target is outside the declared record corpus",
                    focus.where,
                    kind="finding" if self.corpus.closed else "unverified",
                    details=focus.record.location,
                    evidence_json=self.evidence.evidence((), (link,), operation),
                )
            )
            self.evidence.unresolved(relation)
            return
        target_id = target.record.identifier
        if relation.reachability:
            self.evidence.reachable_link(link, operation)
        if relation.ownership:
            incoming = self.evidence.owner_link(link, operation)
            maximum = contract.max_owners
            if (
                maximum is not None
                and len(incoming) > maximum
                and target_id not in self.corpus.shared_set
                and target_id not in self.excess
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
                        evidence_json=self.evidence.evidence((), proof, operation),
                    )
                )
                self.excess.add(target_id)
        if relation.acyclic and target_id not in self.evidence.adjacency.get(
            source_id, {}
        ):
            cycle_path = self.evidence.path(target_id, source_id, operation)
            if cycle_path is not None:
                operation.consume("states", 1 + len(cycle_path))
                identity = frozenset(
                    (
                        source_id,
                        target_id,
                        *(item.source.record.identifier for item in cycle_path),
                    )
                )
                if identity not in self.cycles:
                    issues.append(
                        Issue(
                            "data.topology_cycle",
                            "Selected application paths form a cycle",
                            focus.where,
                            details=focus.record.location,
                            evidence_json=self.evidence.evidence(
                                (),
                                (link, *cycle_path),
                                operation,
                            ),
                        )
                    )
                    self.cycles.add(identity)
            self.evidence.acyclic_link(link, operation)

    def finish(self) -> None:
        """Project absence only under the relevant complete closed evidence."""
        operation, issues, contract = self.issues.budget, self.issues, self.contract
        reached = self.evidence.reached(self.corpus, operation)
        if contract.require_reachable and not self.corpus.roots:
            issues.append(
                Issue(
                    "data.topology_roots_unverified",
                    "No required root record binding was supplied",
                    kind="unverified",
                )
            )
        for focus in self.corpus.nodes.values():
            operation.consume("work")
            identifier = focus.record.identifier
            if contract.require_reachable and identifier not in reached:
                known = (
                    self.corpus.closed
                    and bool(self.corpus.roots)
                    and not self.evidence.reach_unknown
                )
                issues.append(
                    Issue(
                        "data.topology_unreachable",
                        "Record is not established reachable from the selected roots",
                        focus.where,
                        kind="finding" if known else "unverified",
                        details=focus.record.location,
                        evidence_json=self.evidence.evidence((focus,), (), operation),
                    )
                )
            minimum = contract.min_owners
            count = len(self.evidence.owners.get(identifier, {}))
            if (
                minimum is not None
                and identifier not in self.corpus.root_set
                and count < minimum
            ):
                known = self.corpus.closed and not self.evidence.owner_unknown
                issues.append(
                    Issue(
                        "data.topology_owners",
                        "Required incoming ownership is not established",
                        focus.where,
                        kind="finding" if known else "unverified",
                        expected=str(minimum),
                        actual=str(count),
                        details=focus.record.location,
                        evidence_json=self.evidence.evidence((focus,), (), operation),
                    )
                )
        if not self.corpus.complete:
            issues.append(
                Issue(
                    "data.topology_corpus_unverified",
                    "Required record inventory is not declared complete",
                    kind="unverified",
                )
            )


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
    corpus: RecordCorpus | None = None
    evidence: RelationEvidence | None = None
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
            capture_inventory(records, TopologyRecord, operation, count=requested),
        )
        root_ids = cast(tuple[str, ...], capture_inventory(roots, str, operation))
        shared_ids = cast(
            tuple[str, ...], capture_inventory(shared_targets, str, operation)
        )
        if len(set(root_ids)) != len(root_ids) or len(set(shared_ids)) != len(
            shared_ids
        ):
            raise InputError("Topology roots and shared targets must be unique")
        for value in (contract.min_owners, contract.max_owners):
            if value is not None:
                bound_index(value, operation)
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
        reader = read_records(source, operation, graph_name=graph_name, union=union)
        scope, scope_size = _context(reader, operation)
        operation.consume("states")
        operation.consume("bytes", 128)
        corpus = RecordCorpus(reader, root_ids, shared_ids, complete, closure)
        for focus, presence in corpus.admit(captured, operation):
            if not presence.owned:
                issues.append(
                    Issue(
                        "data.topology_binding_unverified",
                        "Blank record binding lacks exact snapshot ownership",
                        focus.where,
                        kind="unverified",
                        details=focus.record.location,
                    )
                )
            else:
                issues.append(
                    Issue(
                        "data.topology_record_missing",
                        "Record has no established occurrence in the selected graph",
                        focus.where,
                        kind="finding"
                        if complete and closure == "closed"
                        else "unverified",
                        details=focus.record.location,
                    )
                )
        operation.consume("states", 2)
        operation.consume("bytes", 256)
        evidence = RelationEvidence(
            scope, scope_size, corpus.unknown_inventory, corpus.unknown_inventory
        )
        policy = _Policy(corpus, evidence, contract, issues)
        for item in evidence.links(corpus, contract, operation):
            if isinstance(item, _Gap):
                policy.unresolved(item)
            else:
                policy.observe(item)
        policy.finish()
    except LimitExceeded as error:
        finished = False
        issues.exhausted(str(error), location="record-topology")
    return Report(
        "record-topology",
        (corpus.checked if corpus is not None else 0)
        + (evidence.checked if evidence is not None else 0),
        tuple(issues),
        requested=requested,
        complete=finished,
        regime="application-topology",
        families=("application-topology",),
    )
