"""One SQLite transaction owns both exact ontology content and allocation history."""

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import cast

from rdflib import Dataset

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    retain_report,
)
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.records.codecs import (
    read_witness,
    witness_data,
)
from bijux_ontology.rdf.records.materialization import materialize
from bijux_ontology.rdf.records.types import PatchUnavailable
from bijux_ontology.rdf.sources.metadata import SnapshotMetadata
from bijux_ontology.rdf.sources.snapshots import PreparedSource

from . import codecs, qualification
from .types import (
    AllocationCommit,
    AllocationPolicy,
    AllocationProposal,
    AllocationSnapshot,
    text,
)


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
    qualification.qualify_draft(draft, operation)
    if (
        data["store"] != draft.store_id
        or data["revision"] != draft.revision
        or policy != base._policy
        or codecs.encode(data, operation) != base._receipt
    ):
        raise InputError("The allocation base is stale or belongs to another store")
    captured, referenced = qualification.candidate_inventory(
        candidate, policy_specification, operation
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
        codecs.assignment_data(item, operation)
        for item in (*base.history, *draft.assignments)
    ]
    operation.consume("work", len(blocked) * (len(blocked).bit_length() + 1))
    updated["unavailable"] = sorted(blocked)
    return updated


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
        policy = codecs.encode(
            qualification.policy_data(self.policy, operation), operation
        )
        captured, unavailable = qualification.collision_inventory(
            source, self.policy, operation
        )
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
        raw = codecs.encode(data, operation)
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
        expected = codecs.encode(
            qualification.policy_data(self.policy, operation), operation
        )
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
        source = materialize(witness, template, operation)
        history = data["history"]
        unavailable = data["unavailable"]
        if not isinstance(history, list) or not isinstance(unavailable, list):
            raise InputError("Malformed allocation histories")
        operation.consume("states", len(history) + len(unavailable))
        assignments = tuple(codecs.read_assignment(item, operation) for item in history)
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
            "_receipt": codecs.encode(data, operation),
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
                    raw = codecs.encode(updated, operation)
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
