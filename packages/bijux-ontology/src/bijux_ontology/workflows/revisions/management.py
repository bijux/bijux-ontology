"""Atomic local activation and exact in-flight semantic revision leases."""

from _thread import LockType
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event, Lock, RLock

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.execution.processes.transport import checkpoint
from bijux_ontology.models.emission.types import ModelUsage, RdfModelUsage
from bijux_ontology.models.persistence.types import PreparationIdentity
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.shacl.execution.session import ShaclSession

from .preparation import named_terms, preparation_capacity, shape_bindings
from .types import PreparedRevision, RevisionResult, RevisionStatus, _RevisionState


@dataclass(slots=True)
class _Retained:
    """One manager-owned revision and its independent lease/run references."""

    revision: str
    retained_bytes: int
    identity: PreparationIdentity
    state: _RevisionState | None
    leases: int = 0
    operations: int = 0
    session: ShaclSession | None = None
    session_lock: LockType = field(default_factory=Lock)
    retiring: bool = False
    cleaning: bool = False
    cleanup_error: BaseException | None = None

    def shape_session(
        self, state: _RevisionState, operation: Operation
    ) -> ShaclSession:
        """Own at most one lazy worker under the retained revision's exact policy."""
        if self.state is not state or state.shacl_preparation is None:
            raise InputError("The retained revision has no owned SHACL preparation")
        # Admission already retained this record, so it cannot retire during this wait.
        while not self.session_lock.acquire(timeout=0.01):
            checkpoint(operation.deadline, operation.cancel)
        try:
            checkpoint(operation.deadline, operation.cancel)
            if state.shacl_preparation.plan.limits != operation.limits:
                raise InputError("Revision SHACL reuse requires the original Limits")
            if self.session is None:
                self.session = ShaclSession._from_preparation(
                    state.shacl_preparation, storage=operation
                )
            return self.session
        finally:
            self.session_lock.release()


class _RevisionOperation:
    """Borrow one retained semantic owner until the enclosing request returns.

    The revision manager owns the reference count. Child checks use this already
    retained owner, so closing the public lease cannot interrupt an admitted
    multi-record request. No mutable revision state is returned to consumers.
    """

    def __init__(self, state: _RevisionState, record: _Retained) -> None:
        self._state: _RevisionState | None = state
        self._record: _Retained | None = record

    def _require_open(self) -> _RevisionState:
        """Deny new child work once the borrowing context has returned."""
        if self._state is None:
            raise InputError("The borrowed revision operation is closed")
        return self._state

    def _close(self) -> None:
        """Release this handle's state before the manager drains its reference."""
        self._state = None
        self._record = None

    @property
    def revision(self) -> str:
        """Identify the exact retained semantic owner for every child receipt."""
        return self._require_open().revision

    @property
    def identity(self) -> PreparationIdentity:
        """Expose the retained immutable input identity without mutable owners."""
        return self._require_open().identity

    def check_emission(
        self, document: object, usages: Sequence[ModelUsage]
    ) -> RevisionResult:
        """Run the existing fresh bounded JSON-LD model checker on this revision."""
        state = self._require_open()
        return RevisionResult(state.revision, state.models.check(document, usages))

    def check_rdf(
        self,
        source: PreparedSource,
        usages: Sequence[RdfModelUsage],
        *,
        graph_name: GraphSelection | None = None,
        union: bool = False,
        budget: Budget | None = None,
    ) -> RevisionResult:
        """Run ordinary owned RDF model checking without optional JSON-LD setup."""
        state = self._require_open()
        return RevisionResult(
            state.revision,
            state.models.check_rdf(
                source, usages, graph_name=graph_name, union=union, budget=budget
            ),
        )

    def validate_shapes(
        self,
        source: PreparedSource,
        *,
        focus_nodes: Sequence[str],
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> RevisionResult:
        """Validate selected named shape/focus pairs with one owned native lifetime."""
        state = self._require_open()
        if state.shacl_preparation is None:
            raise InputError("This revision declares no shape contract")
        operation = Operation(state.models.limits, None, timeout, cancel)
        focus = named_terms(focus_nodes, operation)
        if not focus:
            raise InputError("Leased shape checking needs nonempty named focus")
        _, selected_focus, coverage = shape_bindings(state.roots, focus, operation)
        record = self._record
        if record is None:
            raise InputError("The borrowed revision operation is closed")
        session = record.shape_session(state, operation)
        result = session._validate(
            source,
            focus_nodes=selected_focus,
            graph_name=None,
            union=False,
            source_usages=(),
            scope="dataset",
            cancel=cancel,
            budget=operation,
            coverage=coverage,
        )
        return RevisionResult(state.revision, result.report, result)


class RevisionLease:
    """Check fresh outputs against one exact resource/compiled-contract revision.

    Activation and rollback cannot change this lease's semantic owners. Closing
    a lease denies new operations, while an already running operation retains
    its own reference until it returns. No shared graph or compiled plan escapes.
    """

    _manager: "RevisionManager"
    _record: _Retained
    _closed: bool

    def __init__(self) -> None:
        """Require atomic acquisition through a RevisionManager."""
        raise InputError("Acquire a revision through manager.lease()")

    @classmethod
    def _owned(cls, manager: "RevisionManager", record: _Retained) -> "RevisionLease":
        """Retain a counted reference established under the manager lock."""
        result = object.__new__(cls)
        result._manager = manager
        result._record = record
        result._closed = False
        return result

    @property
    def revision(self) -> str:
        """Return the actual revision used by all of this lease's operations."""
        return self._record.revision

    @property
    def identity(self) -> PreparationIdentity:
        """Return the complete semantic input/engine identity actually leased."""
        return self._record.identity

    @contextmanager
    def _borrow(self) -> Iterator[_RevisionOperation]:
        """Retain one revision operation independently of concurrent lease close."""
        state = self._manager._start(self)
        operation = None
        try:
            operation = _RevisionOperation(state, self._record)
            yield operation
        finally:
            if operation is not None:
                operation._close()
            self._manager._finish(self)

    def check_emission(
        self, document: object, usages: Sequence[ModelUsage]
    ) -> RevisionResult:
        """Run the existing fresh bounded JSON-LD model checker on this revision."""
        with self._borrow() as operation:
            return operation.check_emission(document, usages)

    def check_rdf(
        self,
        source: PreparedSource,
        usages: Sequence[RdfModelUsage],
        *,
        graph_name: GraphSelection | None = None,
        union: bool = False,
        budget: Budget | None = None,
    ) -> RevisionResult:
        """Run ordinary owned RDF model checking without optional JSON-LD setup."""
        with self._borrow() as operation:
            return operation.check_rdf(
                source, usages, graph_name=graph_name, union=union, budget=budget
            )

    def validate_shapes(
        self,
        source: PreparedSource,
        *,
        focus_nodes: Sequence[str],
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> RevisionResult:
        """Validate selected named shape/focus pairs with one owned native lifetime."""
        with self._borrow() as operation:
            return operation.validate_shapes(
                source, focus_nodes=focus_nodes, timeout=timeout, cancel=cancel
            )

    def close(self) -> None:
        """Release this lease while preserving any already running operations."""
        self._manager._release(self)

    def __enter__(self) -> "RevisionLease":
        """Use one coherent revision for the complete application request."""
        with self._manager._lock:
            if self._closed:
                raise InputError("The revision lease is closed")
        return self

    def __exit__(self, *_: object) -> None:
        """Release the request's lease without terminating native work elsewhere."""
        self.close()


class RevisionManager:
    """Own bounded local activation, explicit rollback and close-and-drain.

    Preparation runs separately from publication. Activation uses exact active
    revision compare-and-swap; no implicit eviction, watcher or automatic retry
    changes the current revision. Retired revisions remain rollback candidates
    until explicit retirement. A count/paid-preparation-byte cap rejects further
    activation without disturbing active or leased owners. The byte cap is a
    conservative owned preparation allowance, not a process-memory measurement.
    """

    def __init__(
        self,
        *,
        max_retained: int = 2,
        max_retained_bytes: int | None = None,
        limits: Limits | None = None,
    ) -> None:
        """Set retention bounds without acquiring resources or starting workers."""
        if limits is not None and not isinstance(limits, Limits):
            raise InputError("Revision manager requires Limits")
        selected = limits if limits is not None else Limits()
        cap = (
            selected.max_total_bytes
            if max_retained_bytes is None
            else max_retained_bytes
        )
        if (
            type(max_retained) is not int
            or not 1 <= max_retained <= selected.max_resources
            or type(cap) is not int
            or cap <= 0
        ):
            raise InputError("Revision retention needs positive finite count/byte caps")
        self._limits = selected
        self._maximum = max_retained
        self._byte_cap = cap
        self._lock = RLock()
        self._records: dict[str, _Retained] = {}
        self._active: str | None = None
        self._closing = False

    @contextmanager
    def _locked(self, operation: Operation) -> Iterator[None]:
        """Honor queue deadline/cancel without touching another operation's counters."""
        while True:
            checkpoint(operation.deadline, operation.cancel)
            if self._lock.acquire(timeout=0.01):
                break
        try:
            checkpoint(operation.deadline, operation.cancel)
            yield
        finally:
            self._lock.release()

    def _status(self) -> RevisionStatus:
        """Describe only bounded retained metadata under the existing lock."""
        return RevisionStatus(
            self._active,
            tuple(self._records),
            sum(record.leases for record in self._records.values()),
            sum(record.operations for record in self._records.values()),
            sum(record.retained_bytes for record in self._records.values()),
            self._closing,
            self._closing and not self._records,
        )

    def status(self) -> RevisionStatus:
        """Inspect activation, retained owners and actual draining references."""
        with self._lock:
            return self._status()

    def activate(
        self,
        candidate: PreparedRevision,
        *,
        expected_active: str | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> RevisionStatus:
        """Transfer a verified revision only against the exact active base."""
        operation = Operation(self._limits, None, timeout, cancel)
        if type(candidate) is not PreparedRevision:
            raise InputError("Activation requires an actual checked PreparedRevision")
        if expected_active is not None:
            bound_text(expected_active, operation)
        with self._locked(operation):
            if self._closing:
                raise ResourceError("Revision manager is closing")
            if self._active != expected_active:
                raise ResourceError("Active revision differs from the expected base")
            preparation_capacity(operation, len(self._records) + 4)
            state = candidate._peek()
            if not state.report.verified:
                raise ResourceError("Required preparation did not verify")
            if state.revision in self._records:
                raise InputError("This revision is already retained")
            if (
                len(self._records) >= self._maximum
                or sum(value.retained_bytes for value in self._records.values())
                + state.retained_bytes
                > self._byte_cap
            ):
                raise LimitExceeded(
                    "Retained revision count or preparation bytes exceed their cap"
                )
            record = _Retained(
                state.revision, state.retained_bytes, state.identity, state
            )
            claimed = candidate._claim()
            assert claimed is state
            self._records[state.revision] = record
            self._active = state.revision
            return self._status()

    def rollback(
        self,
        revision: str,
        *,
        expected_active: str | None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> RevisionStatus:
        """Reactivate a retained revision without changing leased owners."""
        operation = Operation(self._limits, None, timeout, cancel)
        bound_text(revision, operation)
        if expected_active is not None:
            bound_text(expected_active, operation)
        with self._locked(operation):
            if self._closing or self._active != expected_active:
                raise ResourceError("Manager is closing or the active base changed")
            if revision not in self._records or self._records[revision].retiring:
                raise ResourceError("Rollback requires a still-retained revision")
            preparation_capacity(operation, len(self._records) + 1)
            self._active = revision
            return self._status()

    def retire(self, revision: str) -> RevisionStatus:
        """Release only an inactive owner with no live lease or operation."""
        operation = Operation(self._limits, None, 30, None)
        bound_text(revision, operation)
        with self._locked(operation):
            record = self._records.get(revision)
            if record is None:
                raise InputError("Revision is not retained")
            if revision == self._active or record.leases or record.operations:
                raise ResourceError("An active or in-use revision cannot be retired")
            preparation_capacity(operation, len(self._records) + 1)
            if record.retiring:
                raise ResourceError("Revision retirement is already in progress")
            record.retiring = True
        self._cleanup(record)
        return self.status()

    def lease(self) -> RevisionLease:
        """Acquire exactly the active revision before starting semantic work."""
        with self._lock:
            if self._closing or self._active is None:
                raise ResourceError("No revision is active for new requests")
            if (
                sum(value.leases for value in self._records.values())
                >= self._limits.max_resources
            ):
                raise LimitExceeded("Revision lease count exceeds max_resources")
            record = self._records[self._active]
            result = RevisionLease._owned(self, record)
            record.leases += 1
            return result

    def _start(self, lease: RevisionLease) -> _RevisionState:
        """Keep an operation alive even when its outer lease closes concurrently."""
        with self._lock:
            if lease._closed:
                raise InputError("The revision lease is closed")
            record = lease._record
            if (
                self._records.get(record.revision) is not record
                or record.state is None
                or record.retiring
            ):
                raise InputError("Revision ownership is no longer retained")
            if (
                sum(value.operations for value in self._records.values())
                >= self._limits.max_resources
            ):
                raise LimitExceeded("Revision operation count exceeds max_resources")
            record.operations += 1
            return record.state

    def _drain(self, record: _Retained) -> bool:
        """Mark an unreferenced owner without dropping its still-live worker quota."""
        if self._closing and not record.leases and not record.operations:
            record.retiring = True
            return True
        return False

    def _cleanup(self, record: _Retained) -> None:
        """Reap outside the manager lock before releasing retained capacity."""
        with self._lock:
            if self._records.get(record.revision) is not record or record.cleaning:
                return
            if record.cleanup_error is not None:
                raise record.cleanup_error
            if not record.retiring or record.leases or record.operations:
                return
            record.cleaning = True
        try:
            if record.session is not None:
                record.session.close()
        except BaseException as error:
            with self._lock:
                record.cleanup_error = error
                record.cleaning = False
            raise
        with self._lock:
            record.session = None
            record.state = None
            self._records.pop(record.revision, None)

    def _finish(self, lease: RevisionLease) -> None:
        """Release one completed operation's counted owner before optional reaping."""
        with self._lock:
            record = lease._record
            record.operations -= 1
            drain = self._drain(record)
        if drain:
            self._cleanup(record)

    def _release(self, lease: RevisionLease) -> None:
        """Idempotently release a lease without touching another request's lifetime."""
        with self._lock:
            if lease._closed:
                return
            lease._closed = True
            record = lease._record
            record.leases -= 1
            drain = self._drain(record)
        if drain:
            self._cleanup(record)

    def close(self) -> RevisionStatus:
        """Deny new requests and drain without killing existing runs."""
        with self._lock:
            self._closing = True
            self._active = None
            records = tuple(
                record for record in self._records.values() if self._drain(record)
            )
        for record in records:
            self._cleanup(record)
        return self.status()

    def __enter__(self) -> "RevisionManager":
        """Own local revision lifetimes in an explicit application scope."""
        return self

    def __exit__(self, *_: object) -> None:
        """Begin deterministic drain while existing request leases remain valid."""
        self.close()
