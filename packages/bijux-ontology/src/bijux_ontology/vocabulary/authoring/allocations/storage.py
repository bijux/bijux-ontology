"""One SQLite transaction owns both exact ontology content and allocation history."""

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Literal as TypingLiteral
from typing import cast

from rdflib import Dataset, URIRef

from bijux_ontology._json import decode_json, object_value, string_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.patches.applying import _materialize
from bijux_ontology.rdf.patches.codecs import (
    read_term,
    read_witness,
    term_data,
    witness_data,
)
from bijux_ontology.rdf.patches.planning import Captured, capture_source
from bijux_ontology.rdf.patches.types import PatchLiteral, PatchUnavailable
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, literal_datatype, literal_term
from bijux_ontology.vocabulary.accessions.recognition import prepare_rules, resolve
from bijux_ontology.vocabulary.accessions.types import AccessionRule
from bijux_ontology.vocabulary.registry.types import IdentifierRegistry

from .types import (
    AllocationAssignment,
    AllocationCommit,
    AllocationDraft,
    AllocationPolicy,
    AllocationProposal,
    AllocationRequest,
    AllocationSnapshot,
    CuratorRange,
    text,
)


def encode(value: object, operation: Budget) -> bytes:
    """Prepay conservative JSON encoding and retained bytes before serialization."""
    size = json_bound(value, operation)
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Allocation artifact exceeds max_bytes")
    operation.consume("bytes", 3 * size)
    operation.consume("work", size)
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def assignment_data(item: AllocationAssignment, operation: Budget) -> list[object]:
    """Retain exact labels and caller keys without inventing term equivalence."""
    if not isinstance(item, AllocationAssignment):
        raise InputError("Expected AllocationAssignment")
    bound_node(item.iri, operation)
    request = item.request
    text(request.key, operation)
    text(request.kind, operation)
    if request.label is not None:
        bound_node(request.label, operation)
    label = (
        PatchLiteral(
            str(request.label),
            str(literal_datatype(request.label)),
            request.label.language,
        )
        if request.label is not None
        else None
    )
    operation.consume("states", 4)
    return [str(item.iri), request.key, request.kind, term_data(label, operation)]


def read_assignment(value: object, operation: Budget) -> AllocationAssignment:
    """Decode a finite assignment through the existing exact RDF term codec."""
    if not isinstance(value, list) or len(value) != 4:
        raise InputError("Malformed allocation assignment")
    for item in value[:3]:
        text(item, operation)
    label = read_term(value[3], operation)
    if label is not None and not isinstance(label, PatchLiteral):
        raise InputError("Allocation labels require literal records")
    if value[2] not in ("class", "concept", "scheme"):
        raise InputError("Unsupported allocated declaration")
    return AllocationAssignment(
        URIRef(string_value(value[0])),
        AllocationRequest(
            string_value(value[1]),
            cast(TypingLiteral["class", "concept", "scheme"], value[2]),
            literal_term(
                label.lexical, datatype=label.datatype, language=label.language
            )
            if label is not None
            else None,
        ),
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
        assignment_data(item, operation)


def committed_data(
    policy_specification: AllocationPolicy,
    proposal: AllocationProposal,
    data: dict[str, object],
    policy: bytes,
    operation: Operation,
) -> dict[str, object]:
    """Validate the complete exact CAS and native candidate acceptance receipt."""
    base, draft, candidate = proposal._base, proposal.draft, proposal.candidate
    assert base is not None and draft is not None and candidate is not None
    qualify_draft(draft, operation)
    if (
        data["store"] != draft.store_id
        or data["revision"] != draft.revision
        or policy != base._policy
        or encode(data, operation) != base._receipt
    ):
        raise InputError("The allocation base is stale or belongs to another store")
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
    captured, referenced = collision_inventory(
        candidate.source, policy_specification, operation
    )
    # The candidate is the exact owned output of apply_rdf_patch plus selected
    # native postchecks. Persist its actual content, not a replayed source input.
    operation.consume(
        "states", len(base.history) + len(draft.assignments) + len(base.unavailable)
    )
    operation.consume(
        "work", len(base.history) + len(draft.assignments) + len(base.unavailable)
    )
    blocked = set(base.unavailable)
    for assignment in draft.assignments:
        if str(assignment.iri) in blocked:
            raise InputError("A proposed identifier is no longer available")
        blocked.add(str(assignment.iri))
    operation.consume("states", len(referenced))
    operation.consume("work", len(referenced))
    blocked.update(referenced)
    updated = dict(data)
    updated["revision"] = draft.revision + 1
    updated["witness"] = witness_data(captured.witness, operation)
    updated["history"] = [
        assignment_data(item, operation) for item in (*base.history, *draft.assignments)
    ]
    operation.consume("work", len(blocked) * (len(blocked).bit_length() + 1))
    updated["unavailable"] = sorted(blocked)
    return updated


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


class AllocationStore:
    """An explicit local transaction owner, never an external RDF file writer.

    The SQLite document is the durable ontology and allocation authority. RDF
    exports are independent snapshots. Callers reopen with the same explicit
    policy; a mismatching policy cannot silently redefine curator ownership.
    """

    def __init__(self, path: Path, policy: AllocationPolicy) -> None:
        """Capture a local filename and explicit policy without creating storage."""
        if not isinstance(path, Path):
            raise InputError("Allocation stores require a pathlib.Path")
        text(str(path), Budget())
        self.path, self.policy = path, policy

    @contextmanager
    def _connection(
        self, operation: Operation, *, create: bool = False
    ) -> Iterator[sqlite3.Connection]:
        """Bound lock waits and interrupt SQLite work on deadline/cancellation."""
        text(str(self.path), operation)
        if not create and not self.path.is_file():
            raise InputError("Allocation store does not exist")
        operation.consume("resources")
        connection = sqlite3.connect(self.path, timeout=0.01, isolation_level=None)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        connection.set_progress_handler(
            lambda: int(
                time.monotonic() >= operation.deadline
                or operation.cancel is not None
                and operation.cancel.is_set()
            ),
            100,
        )
        try:
            while True:
                operation.consume("work")
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error):
                        raise
            yield connection
            operation.consume("work")
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            operation.consume("work")
            raise ResourceError("Allocation storage transaction failed") from error
        except BaseException:
            connection.set_progress_handler(None, 0)
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def create(
        self,
        source: PreparedSource,
        *,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 10.0,
        cancel: Event | None = None,
    ) -> AllocationSnapshot:
        """Initialize exact selected content and unavailable IDs without reuse."""
        operation = Operation(limits, budget, timeout, cancel)
        policy = encode(policy_data(self.policy, operation), operation)
        captured, unavailable = collision_inventory(source, self.policy, operation)
        metadata = SnapshotMetadata.capture(source, operation)
        operation.consume(
            "work", len(unavailable) * (len(unavailable).bit_length() + 1)
        )
        operation.consume("states", len(unavailable))
        data = {
            "version": 1,
            "store": uuid.uuid4().hex,
            "revision": 0,
            "policy": policy.decode("ascii"),
            "witness": witness_data(captured.witness, operation),
            "metadata": [
                metadata.document_iri,
                metadata.effective_url,
                metadata.literal_fidelity,
            ],
            "unavailable": sorted(unavailable),
            "history": [],
        }
        raw = encode(data, operation)
        result = self._snapshot(data, policy, operation)
        with self._connection(operation, create=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS allocation ("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "revision INTEGER NOT NULL, body BLOB NOT NULL)"
            )
            if connection.execute("SELECT 1 FROM allocation").fetchone() is not None:
                raise InputError("Allocation store is already initialized")
            connection.execute("INSERT INTO allocation VALUES(1,0,?)", (raw,))
        return result

    def _load(
        self, connection: sqlite3.Connection, operation: Operation
    ) -> tuple[dict[str, object], bytes]:
        """Inspect declared storage size before SQLite copies a BLOB into Python."""
        info = connection.execute(
            "SELECT revision,typeof(body),length(body) "
            "FROM allocation WHERE singleton=1"
        ).fetchone()
        if info is None or info[1] != "blob" or type(info[2]) is not int:
            raise InputError("Malformed allocation storage")
        size = info[2]
        if size > operation.limits.max_bytes:
            raise LimitExceeded("Stored allocation content exceeds max_bytes")
        operation.consume("bytes", size)
        operation.consume("work", size + 1)
        operation.consume("bytes", 128 * size + 64)
        operation.consume("states", size)
        raw = connection.execute(
            "SELECT body FROM allocation WHERE singleton=1"
        ).fetchone()[0]
        operation.consume("work", 8 * size + 1)
        data = object_value(
            decode_json(
                raw,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
            ),
            location="allocation",
        )
        if (
            set(data)
            != {
                "version",
                "store",
                "revision",
                "policy",
                "witness",
                "metadata",
                "unavailable",
                "history",
            }
            or data["version"] != 1
            or type(data["revision"]) is not int
            or data["revision"] != info[0]
        ):
            raise InputError("Unsupported or inconsistent allocation storage")
        expected = encode(policy_data(self.policy, operation), operation)
        if data["policy"] != expected.decode("ascii"):
            raise InputError(
                "Allocation policy differs from its durable ownership receipt"
            )
        return data, expected

    def _snapshot(
        self, data: dict[str, object], policy: bytes, operation: Operation
    ) -> AllocationSnapshot:
        """Restore exact portable witness terms into one fresh owner."""
        witness = read_witness(data["witness"], operation)
        metadata = data["metadata"]
        if (
            not isinstance(metadata, list)
            or len(metadata) != 3
            or any(
                value is not None and not isinstance(value, str) for value in metadata
            )
            or metadata[2] not in ("source_terms", "supplied_terms")
        ):
            raise InputError("Malformed allocation origin/fidelity receipt")
        for value in metadata:
            if value is not None:
                text(value, operation)
        template = PreparedSource._from_owned(
            Dataset(),
            is_dataset=witness.kind != "graph",
            document_iri=cast(str | None, metadata[0]),
            effective_url=cast(str | None, metadata[1]),
            literal_fidelity=cast(str, metadata[2]),
        )
        source = _materialize(witness, template, operation)
        history = data["history"]
        unavailable = data["unavailable"]
        if not isinstance(history, list) or not isinstance(unavailable, list):
            raise InputError("Malformed allocation histories")
        operation.consume("states", len(history) + len(unavailable))
        assignments = tuple(read_assignment(item, operation) for item in history)
        blocked = frozenset(text(item, operation) for item in unavailable)
        store = text(data["store"], operation)
        if any(str(item.iri) not in blocked for item in assignments):
            raise InputError("Allocation history is missing a never-reuse identifier")
        result = object.__new__(AllocationSnapshot)
        for name, value in {
            "store_id": store,
            "revision": data["revision"],
            "source": source,
            "witness": witness,
            "history": assignments,
            "unavailable": blocked,
            "_policy": policy,
            "_metadata": tuple(metadata),
            "_receipt": encode(data, operation),
            "_scope": SnapshotMetadata.capture(source, operation),
        }.items():
            object.__setattr__(result, name, value)
        return result

    def snapshot(
        self,
        *,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 10.0,
        cancel: Event | None = None,
    ) -> AllocationSnapshot:
        """Load a bounded, fresh base suitable for review and later CAS."""
        operation = Operation(limits, budget, timeout, cancel)
        with self._connection(operation) as connection:
            data, policy = self._load(connection, operation)
            return self._snapshot(data, policy, operation)

    def commit(
        self,
        proposal: AllocationProposal,
        *,
        accept: bool = False,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 10.0,
        cancel: Event | None = None,
    ) -> AllocationCommit:
        """Atomically commit checked content and history under an exact base CAS.

        User callbacks run while preparing, outside the SQLite write lock. A
        candidate is immutable owned RDF; its metadata receipt is checked again.
        Cancellation or any rejected postcondition leaves the durable revision
        and ontology unchanged. No separately exported file is part of this CAS.
        """
        operation = Operation(limits, budget, timeout, cancel)
        issues = IssueCollector(operation)
        revision = None
        completed = 0
        children: list[Report] = []
        try:
            if type(accept) is not bool or not isinstance(proposal, AllocationProposal):
                raise InputError(
                    "Commit requires a prepared proposal and boolean acceptance"
                )
            issues.retain_completed(proposal.report)
            retain_report(proposal.report, operation, issues)
            children.append(proposal.report)
            if (
                not accept
                or not proposal.report.verified
                or proposal.draft is None
                or proposal.candidate is None
                or proposal._base is None
            ):
                issues.append(
                    Issue(
                        "vocabulary.allocation_unverified",
                        "Commit requires acceptance and checked term additions",
                        kind="unverified",
                    )
                )
            else:
                with self._connection(operation) as connection:
                    data, policy = self._load(connection, operation)
                    updated = committed_data(
                        self.policy, proposal, data, policy, operation
                    )
                    raw = encode(updated, operation)
                    operation.consume("work")
                    connection.execute(
                        "UPDATE allocation SET revision=?,body=? "
                        "WHERE singleton=1 AND revision=?",
                        (updated["revision"], raw, data["revision"]),
                    )
                    completed = len(proposal.draft.assignments)
                    # Reserve final response fields before the atomic commit.
                    operation.consume("states", 2)
                    operation.consume("bytes", 128)
                revision = cast(int, updated["revision"])
        except LimitExceeded as error:
            issues.exhausted(str(error))
        except (ResourceError, PatchUnavailable) as error:
            try:
                issues.append(
                    Issue(
                        "vocabulary.allocation_unverified",
                        str(error),
                        kind="unverified",
                    )
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
        return AllocationCommit(
            revision,
            Report(
                "vocabulary.allocation_commit",
                completed if revision is not None else 0,
                tuple(issues),
                requested=max(1, completed),
                complete=revision is not None,
                children=tuple(children),
            ),
        )
