"""Qualify exact allocation policy, drafts and checked candidate inventories."""

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.migrations.candidates import MigrationCandidate
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.records.types import PatchLiteral, PatchUnavailable
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.vocabulary.accessions.recognition import prepare_rules, resolve
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from . import codecs
from .types import (
    AllocationAssignment,
    AllocationDraft,
    AllocationPolicy,
    CuratorRange,
    text,
)


def qualify_draft(draft: AllocationDraft, operation: Budget) -> None:
    """Qualify explicit review claims before comparison or allocation adoption."""
    if not isinstance(draft, AllocationDraft):
        raise InputError("Expected AllocationDraft")
    for value in (draft.store_id, draft.owner, draft.prefix):
        text(value, operation)
    if type(draft.revision) is not int or not 0 <= draft.revision < 2**63 - 1:
        raise InputError("Allocation revisions require a bounded nonnegative integer")
    for item in capture_items(draft.assignments, AllocationAssignment, operation):
        codecs.assignment_data(item, operation)


def policy_data(policy: AllocationPolicy, operation: Budget) -> dict[str, object]:
    """Qualify explicit registry ownership and actual generated identifier spaces."""
    if not isinstance(policy, AllocationPolicy):
        raise InputError("Expected AllocationPolicy")
    registry, _rules = prepare_rules(policy.registry, policy.alternate_rules, operation)
    ranges = capture_items(policy.ranges, CuratorRange, operation)
    if not ranges:
        raise InputError("Allocation requires at least one explicitly owned range")
    selected = []
    intervals: list[tuple[str, str, str]] = []
    for item in ranges:
        owner, prefix = text(item.owner, operation), text(item.prefix, operation)
        CuratorRange(owner, prefix, item.start, item.end, item.digits)
        owned = False
        for contribution in registry.contributions:
            operation.consume("work", len(owner) + len(prefix) + 1)
            if contribution.owner == owner and prefix in contribution.prefixes:
                owned = True
        if not owned:
            raise InputError("An allocation range requires a contributed owner/prefix")
        operation.consume("bytes", len(prefix.encode("utf-8")) + 1)
        namespace = registry.expand_curie(prefix + ":")
        width = len(namespace.encode("utf-8")) + item.digits
        if width > operation.limits.max_bytes:
            raise LimitExceeded("Padded identifier exceeds max_bytes")
        operation.consume("work", 2 * item.digits)
        operation.consume("bytes", 2 * width)
        lower = namespace + str(item.start).zfill(item.digits)
        upper = namespace + str(item.end).zfill(item.digits)
        for other_namespace, other_lower, other_upper in intervals:
            operation.consume("work", len(lower) + len(other_lower) + 1)
            if len(lower) != len(other_lower):
                continue
            common = (
                namespace[len(other_namespace) :]
                if namespace.startswith(other_namespace)
                else other_namespace[len(namespace) :]
                if other_namespace.startswith(namespace)
                else None
            )
            if (
                common is not None
                and (not common or common.isascii() and common.isdigit())
                and max(lower, other_lower) <= min(upper, other_upper)
            ):
                raise InputError("Selected curator ranges overlap in identifier space")
        operation.consume("states", 2)
        intervals.append((namespace, lower, upper))
        selected.append([owner, prefix, item.start, item.end, item.digits])
    for collection in (policy.reserved, policy.retired, policy.alternate_predicates):
        for node in capture_items(collection, URIRef, operation):
            bound_node(node, operation)
    references = 32 + 8 * len(registry.contributions) + 8 * len(ranges)
    for contribution in registry.contributions:
        operation.consume("work")
        references += len(contribution.aliases)
    for rule in policy.alternate_rules:
        operation.consume("work")
        references += 8 + len(rule.alternates)
    operation.consume("states", references)
    operation.consume("bytes", 64 * references)
    return {
        "registry": [
            [item.namespace, item.canonical_prefix, item.owner, list(item.aliases)]
            for item in registry.contributions
        ],
        "canonical": list(registry.canonical_prefixes),
        "ranges": selected,
        "reserved": [str(node) for node in policy.reserved],
        "retired": [str(node) for node in policy.retired],
        "alternate_predicates": [str(node) for node in policy.alternate_predicates],
        "rules": [
            [
                rule.namespace,
                rule.pattern,
                rule.case_policy,
                rule.check_digit,
                rule.checksum_separators,
                [alternate.template for alternate in rule.alternates],
            ]
            for rule in policy.alternate_rules
        ],
    }


def reserve_identifier(
    node: URIRef,
    unavailable: set[str],
    registry: IdentifierRegistry,
    rules: dict[str, AccessionRule],
    operation: Budget,
) -> None:
    """Recognize declared alternate IRIs for every collision inventory role."""
    bound_node(node, operation)
    operation.consume("states")
    unavailable.add(str(node))
    if not rules:
        return
    rule, local, qualified, ambiguous = resolve(
        str(node), "iri", registry, rules, operation
    )
    if not qualified or ambiguous:
        raise PatchUnavailable(
            "Required alternate identifier recognition is unresolved"
        )
    if rule is not None and local is not None:
        size = len(rule.namespace.encode("utf-8")) + len(local.encode("utf-8"))
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Alternate identifier exceeds max_bytes")
        operation.consume("bytes", size)
        operation.consume("states")
        unavailable.add(rule.namespace + local)


def collision_inventory(
    source: PreparedSource, policy: AllocationPolicy, operation: Operation
) -> tuple[Captured, set[str]]:
    """Reserve every selected named occurrence and explicit alternate accession."""
    captured = capture_source(source, "allocation:", operation)
    registry, rules = prepare_rules(policy.registry, policy.alternate_rules, operation)
    unavailable: set[str] = set()

    for collection in (policy.reserved, policy.retired):
        for node in collection:
            operation.consume("work")
            reserve_identifier(node, unavailable, registry, rules, operation)
    predicates = set(capture_items(policy.alternate_predicates, URIRef, operation))
    for quad in captured.witness.quads:
        for term in (quad.subject, quad.predicate, quad.value, quad.graph):
            operation.consume("work")
            if isinstance(term, URIRef):
                reserve_identifier(term, unavailable, registry, rules, operation)
            elif isinstance(term, PatchLiteral):
                bound_text(term.datatype, operation)
                reserve_identifier(
                    URIRef(term.datatype), unavailable, registry, rules, operation
                )
        if quad.predicate in predicates:
            value = quad.value
            if isinstance(value, PatchLiteral):
                # These explicitly selected predicates carry identifier spellings,
                # never human labels. IRI versus CURIE intent follows RDF term kind.
                spelling = text(value.lexical, operation)
                operation.consume("work", len(spelling) + len(registry.bindings))
                binding = dict(registry.bindings).get(spelling.split(":", 1)[0])
                if binding is None:
                    raise PatchUnavailable(
                        "Alternate CURIE namespace is not registered"
                    )
                upper = len(binding.encode("utf-8")) + len(spelling.encode("utf-8"))
                if upper > operation.limits.max_bytes:
                    raise LimitExceeded("Alternate identifier exceeds max_bytes")
                operation.consume("bytes", upper)
                canonical = registry.expand_curie(spelling)
                operation.consume("bytes", len(canonical.encode("utf-8")))
                operation.consume("states")
                unavailable.add(canonical)
            elif not isinstance(value, URIRef):
                raise PatchUnavailable(
                    "Anonymous alternate identifiers are unsupported"
                )
    for graph in captured.witness.graphs:
        if isinstance(graph, URIRef):
            reserve_identifier(graph, unavailable, registry, rules, operation)
    return captured, unavailable


def candidate_inventory(
    candidate: MigrationCandidate, policy: AllocationPolicy, operation: Operation
) -> tuple[Captured, set[str]]:
    """Requalify the exact checked source without acquiring commit authority."""
    if (
        candidate.source is None
        or candidate.source is not candidate._checked_source
        or candidate._metadata is None
        or not candidate._metadata.same_scope(
            SnapshotMetadata.capture(candidate.source, operation)
        )
    ):
        raise PatchUnavailable(
            "Allocation candidate no longer has its checked metadata receipt"
        )
    if not candidate.report.verified or candidate.patch is None:
        raise PatchUnavailable(
            "Allocation candidate lacks complete required postchecks"
        )
    return collision_inventory(candidate.source, policy, operation)
