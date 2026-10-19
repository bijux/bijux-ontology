"""Checked native additions and explicit reconciliation of allocation proposals."""

import time
from collections.abc import Sequence
from threading import Event
from typing import cast

from rdflib import BNode, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.migrations.candidates import MigrationCheck, finish_migration
from bijux_ontology.rdf.patches.applying import apply_rdf_patch
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.rdf.patches.types import DatasetPatch, PatchBlankBinding
from bijux_ontology.rdf.records.types import (
    PatchLiteral,
    PatchQuad,
    PatchResource,
    PatchUnavailable,
    SnapshotWitness,
)
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node, literal_datatype
from bijux_ontology.vocabulary.accessions.recognition import prepare_rules

from . import codecs, qualification
from .storage import AllocationStore
from .types import (
    AllocationAssignment,
    AllocationDraft,
    AllocationProposal,
    AllocationRequest,
    AllocationSnapshot,
    CuratorRange,
    text,
)


def _requests(
    values: Sequence[AllocationRequest], operation: Budget
) -> tuple[AllocationRequest, ...]:
    """Requalify every caller intent under the actual preparation allowance."""
    result = capture_items(values, AllocationRequest, operation)
    keys = set()
    for item in result:
        text(item.key, operation)
        if item.kind not in ("class", "concept", "scheme"):
            raise InputError("Unsupported allocated declaration")
        if item.label is not None:
            bound_node(item.label, operation)
        operation.consume("states")
        if item.key in keys:
            raise InputError("Allocation request keys must be distinct")
        keys.add(item.key)
    if not result:
        raise PatchUnavailable("Allocation requires at least one term request")
    return result


def _range(
    store: AllocationStore, owner: str, prefix: str, operation: Budget
) -> tuple[CuratorRange, str]:
    """Require one exact owner/prefix range; aliases never grant extra ownership."""
    text(owner, operation)
    text(prefix, operation)
    selected = []
    for item in store.policy.ranges:
        operation.consume("work")
        if item.owner == owner and item.prefix == prefix:
            selected.append(item)
    if len(selected) != 1:
        raise InputError(
            "Select one explicitly owned curator range by owner and prefix"
        )
    namespace = store.policy.registry.expand_curie(prefix + ":")
    text(namespace, operation)
    return selected[0], namespace


def _iri(item: CuratorRange, namespace: str, number: int, operation: Budget) -> URIRef:
    """Prepay padded decimal construction and exact named identifier storage."""
    operation.consume("work", item.digits + len(namespace) + 1)
    operation.consume("bytes", item.digits + len(namespace.encode("utf-8")))
    return URIRef(namespace + str(number).zfill(item.digits))


def prepare_allocations(
    store: AllocationStore,
    base: AllocationSnapshot,
    requests: Sequence[AllocationRequest],
    *,
    owner: str,
    prefix: str,
    graph: URIRef | BNode | None | Absent = ABSENT,
    proposed: AllocationDraft | None = None,
    after_checks: Sequence[MigrationCheck],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 10.0,
    cancel: Event | None = None,
) -> AllocationProposal:
    """Prepare all-or-nothing native declarations without reserving any IDs.

    Named OWL classes, SKOS concepts and schemes receive only explicit rdf:type
    and optional exact rdfs:label/skos:prefLabel additions. No OWL inference or
    external authority follows. Postchecks are trusted ordinary Python callbacks;
    their runtime is caller-owned. Original content and durable state stay intact.
    An imported review draft specifies exact assignments and is never renumbered.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    candidate = None
    draft = None
    checked = 0
    children: list[Report] = []
    try:
        if not isinstance(store, AllocationStore) or not isinstance(
            base, AllocationSnapshot
        ):
            raise InputError("Allocation preparation requires a store and loaded base")
        current_policy = codecs.encode(
            qualification.policy_data(store.policy, operation), operation
        )
        if current_policy != base._policy:
            raise InputError("Allocation base belongs to a different policy")
        selected, namespace = _range(store, owner, prefix, operation)
        metadata = SnapshotMetadata.capture(base.source, operation)
        if not base._scope.same_scope(metadata):
            raise PatchUnavailable(
                "Allocation base scope differs from its loaded owner"
            )
        captured = capture_source(base.source, "base:", operation)
        if (
            metadata.document_iri,
            metadata.effective_url,
            metadata.literal_fidelity,
        ) != base._metadata:
            raise PatchUnavailable("Allocation base metadata changed since loading")
        supplied = _requests(requests, operation)
        operation.consume("states", len(base.history) + len(base.unavailable))
        operation.consume("work", len(base.history) + len(base.unavailable))
        previous_keys = {item.request.key for item in base.history}
        blocked = set(base.unavailable)
        registry, rules = prepare_rules(
            store.policy.registry, store.policy.alternate_rules, operation
        )
        for request in supplied:
            if request.label is not None:
                qualification.reserve_identifier(
                    literal_datatype(request.label), blocked, registry, rules, operation
                )
        assignments = []
        if proposed is not None:
            qualification.qualify_draft(proposed, operation)
            if (
                proposed.store_id,
                proposed.revision,
                proposed.owner,
                proposed.prefix,
            ) != (base.store_id, base.revision, owner, prefix):
                raise InputError(
                    "Review draft does not match the selected exact base/range"
                )
            if len(proposed.assignments) != len(supplied):
                raise InputError("Review draft does not cover every requested term")
        number = selected.start
        for index, request in enumerate(supplied):
            operation.consume("work")
            if request.key in previous_keys:
                raise InputError(
                    "A committed request key cannot receive another identifier"
                )
            if proposed is None:
                while number <= selected.end:
                    iri = _iri(selected, namespace, number, operation)
                    number += 1
                    if str(iri) not in blocked:
                        break
                else:
                    raise InputError("The selected curator range is exhausted")
                assignment = AllocationAssignment(iri, request)
            else:
                assignment = proposed.assignments[index]
                if assignment.request != request:
                    raise InputError("Review draft changes the requested term intent")
                spelling = str(assignment.iri)
                operation.consume("work", len(namespace) + len(spelling) + 1)
                local = (
                    spelling[len(namespace) :] if spelling.startswith(namespace) else ""
                )
                if not (
                    len(local) == selected.digits
                    and local.isascii()
                    and local.isdigit()
                    and selected.start <= int(local) <= selected.end
                ):
                    raise InputError(
                        "Proposed identifier lies outside its owned padded range"
                    )
            if str(assignment.iri) in blocked:
                raise InputError(
                    "Identifier is existing, reserved, retired or allocated"
                )
            operation.consume("states", 2)
            blocked.add(str(assignment.iri))
            assignments.append(assignment)
        graph_ref: PatchResource | None
        if not base.source.is_dataset:
            if graph not in (ABSENT, None):
                raise InputError(
                    "A graph allocation cannot select a named dataset graph"
                )
            graph_ref = None
        else:
            chosen = base.source.graph_name if graph is ABSENT else graph
            if chosen is None and graph is ABSENT:
                raise PatchUnavailable(
                    "Dataset allocation requires an explicit physical graph"
                )
            if chosen == "@default":
                chosen = None
            if chosen is None:
                graph_ref = None
            elif isinstance(chosen, BNode):
                graph_ref = captured.reference(chosen)
            elif isinstance(chosen, str):
                bound_text(chosen, operation)
                bound_node(URIRef(chosen), operation)
                graph_ref = URIRef(chosen)
            else:
                raise InputError(
                    "Allocation graph must be an exact resource or default"
                )
            if (
                captured.witness.kind == "named"
                and graph_ref != captured.witness.graph_selection
                or captured.witness.kind == "default"
                and graph_ref is not None
            ):
                raise InputError("Allocation cannot escape its pinned graph")
            if graph_ref is not None and graph_ref not in captured.witness.graphs:
                raise InputError("Allocation requires an existing selected graph")
        additions = []
        for assignment in assignments:
            operation.consume("states", 2)
            operation.consume("triples")
            kind = {
                "class": OWL.Class,
                "concept": SKOS.Concept,
                "scheme": SKOS.ConceptScheme,
            }[assignment.request.kind]
            additions.append(PatchQuad(assignment.iri, RDF.type, kind, graph_ref))
            label = assignment.request.label
            if label is not None:
                operation.consume("triples")
                additions.append(
                    PatchQuad(
                        assignment.iri,
                        RDFS.label
                        if assignment.request.kind == "class"
                        else SKOS.prefLabel,
                        PatchLiteral(
                            str(label), str(literal_datatype(label)), label.language
                        ),
                        graph_ref,
                    )
                )
        operation.consume("states", len(captured.witness.quads) + len(additions))
        target = SnapshotWitness(
            captured.witness.kind,
            captured.witness.graph_selection,
            (*captured.witness.quads, *additions),
            captured.witness.graphs,
        )
        patch = DatasetPatch(captured.witness, target, additions=tuple(additions))
        operation.consume("states", len(captured.owned))
        bindings_by_token: dict[str, PatchBlankBinding] = {}
        for node, reference in captured.owned.values():
            bindings_by_token.setdefault(
                reference.token, PatchBlankBinding(reference.token, node)
            )
        bindings = tuple(bindings_by_token.values())
        transformed = apply_rdf_patch(
            base.source,
            patch,
            blank_bindings=bindings,
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        candidate = finish_migration(
            base.source,
            transformed,
            patch,
            before_checks=(),
            after_checks=after_checks,
            budget=operation,
        )
        issues.retain_completed(candidate.report)
        children.append(candidate.report)
        retain_report(candidate.report, operation, issues)
        if candidate.report.verified:
            operation.consume("states", len(assignments) + 1)
            draft = AllocationDraft(
                base.store_id, base.revision, owner, prefix, tuple(assignments)
            )
            checked = len(assignments)
    except LimitExceeded as error:
        if candidate is not None:
            issues.retain_completed(candidate.report)
        issues.exhausted(str(error))
    except (ResourceError, PatchUnavailable) as error:
        try:
            issues.append(
                Issue("vocabulary.allocation_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    except InputError as error:
        try:
            issues.append(
                Issue("vocabulary.allocation_rejected", str(error), kind="finding")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    result = object.__new__(AllocationProposal)
    for name, value in {
        "draft": draft,
        "candidate": candidate,
        "report": Report(
            "vocabulary.allocation_prepare",
            checked,
            tuple(issues),
            requested=max(1, checked),
            complete=draft is not None,
            children=tuple(children),
        ),
        "_base": base if isinstance(base, AllocationSnapshot) else None,
    }.items():
        object.__setattr__(result, name, value)
    return result


def allocation_draft_data(
    draft: AllocationDraft, *, budget: Budget | None = None
) -> dict[str, object]:
    """Export only finite review intentions; no executable checks or reports."""
    operation = budget if budget is not None else Budget()
    qualification.qualify_draft(draft, operation)
    return {
        "version": 1,
        "store": draft.store_id,
        "revision": draft.revision,
        "owner": draft.owner,
        "prefix": draft.prefix,
        "assignments": [
            codecs.assignment_data(item, operation) for item in draft.assignments
        ],
    }


def read_allocation_draft(
    value: object, *, budget: Budget | None = None
) -> AllocationDraft:
    """Import typed review intentions without granting them commit authority."""
    operation = budget if budget is not None else Budget()
    if (
        not isinstance(value, dict)
        or len(value) != 6
        or set(value)
        != {"version", "store", "revision", "owner", "prefix", "assignments"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["assignments"], list)
    ):
        raise InputError("Malformed allocation review artifact")
    operation.consume("states", len(value["assignments"]))
    assignments = tuple(
        codecs.read_assignment(item, operation) for item in value["assignments"]
    )
    result = AllocationDraft(
        text(value["store"], operation),
        cast(int, value["revision"]),
        text(value["owner"], operation),
        text(value["prefix"], operation),
        assignments,
    )
    qualification.qualify_draft(result, operation)
    return result


def reconcile_allocations(
    drafts: Sequence[AllocationDraft],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Report conflicting branch proposals without choosing a semantic winner.

    The comparison is explicit request identity and exact kind/label intent, not
    a declaration that matching labels identify the same scientific concept.
    Different bases remain required unknown; complete pair conflicts are retained.
    """
    operation = budget if budget is not None else Budget(limits)
    if limits is not None and limits != operation.limits:
        raise InputError("Reconciliation limits conflict with shared Budget")
    issues = IssueCollector(operation)
    checked = 0
    try:
        supplied = capture_items(drafts, AllocationDraft, operation)
        if not supplied:
            raise PatchUnavailable("Reconciliation requires at least one draft")
        identities: dict[URIRef, AllocationRequest] = {}
        requests: dict[str, AllocationAssignment] = {}
        bases = set()
        for draft in supplied:
            qualification.qualify_draft(draft, operation)
            operation.consume("states")
            bases.add((draft.store_id, draft.revision))
            for item in draft.assignments:
                operation.consume("work")
                operation.consume("states", 2)
                previous = identities.get(item.iri)
                existing = requests.get(item.request.key)
                if (
                    previous is not None
                    and previous != item.request
                    or existing is not None
                    and existing != item
                ):
                    issues.append(
                        Issue(
                            "vocabulary.allocation_conflict",
                            "Review proposals conflict on identifier or term intent",
                            str(item.iri),
                            kind="finding",
                            actual=item.request.key,
                        )
                    )
                identities[item.iri] = item.request
                requests[item.request.key] = item
                checked += 1
        if len(bases) != 1:
            issues.append(
                Issue(
                    "vocabulary.allocation_unverified",
                    "Review proposals do not share one exact store revision",
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except PatchUnavailable as error:
        issues.append(
            Issue("vocabulary.allocation_unverified", str(error), kind="unverified")
        )
    return Report(
        "vocabulary.allocation_reconciliation",
        checked,
        tuple(issues),
        requested=max(1, checked),
    )
