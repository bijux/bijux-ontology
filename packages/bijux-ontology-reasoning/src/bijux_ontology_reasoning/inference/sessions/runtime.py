"""One qualified native lifetime, binding and bounded request exchange."""

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from typing import Any, Self

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.execution.processes.sessions import FramedSession
from bijux_ontology.execution.processes.transport import checkpoint

from ...backend.owlapi.bundles import qualify_backend
from ...backend.owlapi.execution import qualify_capability
from ...backend.owlapi.protocol import result_counter, result_text
from ...backend.owlapi.receipts import (
    QualifiedReceipt,
    qualify_reasoning_result,
    reasoning_header,
)
from ...backend.types import BackendCapability
from .preparation import encode_request, stage_theory
from .types import PreparedReasoningTheory, SessionPreparation


class NativeSession:
    """One fixed theory and owned JVM; requests never reuse cached answers.

    Explicit binding replaces a captured theory revision. Changing only an
    obligation recomputes its answer without reloading/classifying that theory.
    Failed native lifetimes are discarded; the next independent request may
    replay the fixed captured input, paying its own preparation costs again.
    """

    def __init__(
        self,
        theory: PreparedReasoningTheory,
        *,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        heap_mb: int = 256,
        cancel: Event | None = None,
    ) -> None:
        """Prepare eagerly with explicit heap and optional cumulative host Budget."""
        operation = Operation(limits, budget, timeout, cancel)
        if type(theory) is not PreparedReasoningTheory:
            raise InputError(
                "Session construction requires a captured reasoning theory"
            )
        if type(heap_mb) is not int or not 32 <= heap_mb <= 4096:
            raise InputError("OWL heap_mb must be an integer between 32 and 4096")
        self._limits = operation.limits
        self._budget = budget
        self._heap_mb = heap_mb
        self._closed = Event()
        self._lock = Lock()
        self._transport: FramedSession | None = None
        self._storage: TemporaryDirectory[str] | None = None
        self._request: dict[str, Any] = {}
        self._preparation = b""
        self._ready = False
        self._prior_launches = 0
        self._classifications = 0
        self._theory: PreparedReasoningTheory | None = None
        self.preparation = SessionPreparation(
            "",
            None,
            None,
            None,
            Report("owl.reasoning_preparation", requested=1, complete=False),
        )
        self.bind(theory, budget=budget, timeout=timeout, cancel=cancel)

    @property
    def theory(self) -> PreparedReasoningTheory:
        """Expose the immutable captured binding, never a mutable native ontology."""
        if self._theory is None:
            raise ResourceError("Reasoning session has no captured theory")
        return self._theory

    @property
    def launches(self) -> int:
        """Count actual process launches across explicit bindings and recovery."""
        return self._prior_launches + (
            self._transport.launches if self._transport is not None else 0
        )

    @property
    def classifications(self) -> int:
        """Count acknowledged actual class-hierarchy precomputation calls."""
        return self._classifications

    @property
    def report(self) -> Report:
        """Return the last actual preparation report, independent of query truth."""
        return self.preparation.report

    def __enter__(self) -> Self:
        """Keep the owned process available until deterministic context exit."""
        return self

    def __exit__(self, *_: object) -> None:
        """Release the native reasoner and every owned pipe/process descendant."""
        self.close()

    def close(self) -> None:
        """Cancel an active request and permanently close the session."""
        self._closed.set()
        if self._transport is not None:
            self._transport.close()
        if self._storage is not None:
            self._storage.cleanup()
            self._storage = None
        self._ready = False

    def _operation(
        self,
        limits: Limits | None,
        budget: Budget | None,
        timeout: float,
        cancel: Event | None,
    ) -> Operation:
        """Keep fixed cumulative counters while allowing independent default jobs."""
        if (
            self._budget is not None
            and budget is not None
            and budget is not self._budget
        ):
            raise InputError("A fixed session Budget cannot be replaced per request")
        chosen = self._budget if self._budget is not None else budget
        return Operation(
            limits
            if limits is not None
            else chosen.limits
            if chosen is not None
            else self._limits,
            chosen,
            timeout,
            cancel,
        )

    @contextmanager
    def _locked(self, operation: Operation) -> Iterator[None]:
        """Bound queue waiting without cancelling another request's native owner."""
        while True:
            checkpoint(operation.deadline, operation.cancel)
            remaining = max(0.0, operation.deadline - time.monotonic())
            if self._lock.acquire(timeout=min(0.01, remaining)):
                break
        try:
            checkpoint(operation.deadline, operation.cancel)
            yield
        finally:
            self._lock.release()

    def _command(self, directory: Path, operation: Operation) -> tuple[str, ...]:
        """Use the exact pinned ordinary backend and the same bounded JVM policy."""
        backend = self.theory.backend
        paths = []
        operation.consume("states", len(backend.jars) + 20)
        operation.consume("bytes", 1024 + 32 * len(backend.jars))
        for name in backend.jars:
            bound_text(name, operation)
            path = str(backend.directory / name)
            bound_text(path, operation)
            paths.append(path)
        command = (
            str(backend.java),
            f"-Xmx{self._heap_mb}m",
            "-Xss512k",
            "-XX:MaxDirectMemorySize=64m",
            "-XX:MaxMetaspaceSize=128m",
            "-XX:-HeapDumpOnOutOfMemoryError",
            "-XX:-UsePerfData",
            f"-XX:HeapDumpPath={directory / 'heap.hprof'}",
            f"-Djava.io.tmpdir={directory}",
            f"-XX:ErrorFile={directory / 'jvm-error.log'}",
            "-cp",
            os.pathsep.join(paths),
            "org.bijux.ontology.sessions.PersistentWorker",
            str(directory),
            str(self._limits.max_bytes),
        )
        for value in command:
            bound_text(value, operation)
        return command

    def bind(
        self,
        theory: PreparedReasoningTheory,
        *,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float = 30,
        cancel: Event | None = None,
    ) -> Report:
        """Invalidate preparation for changed captured input or backend semantics."""
        operation = self._operation(limits, budget, timeout, cancel)
        issues = IssueCollector(operation)
        children: list[Report] = []
        try:
            with self._locked(operation):
                try:
                    if self._closed.is_set():
                        raise ResourceError("Reasoning session is closed")
                    if type(theory) is not PreparedReasoningTheory:
                        raise InputError("Bind an ordinary captured reasoning theory")
                    if (
                        self._theory is not None
                        and theory.identity == self._theory.identity
                        and self._ready
                    ):
                        qualify_backend(theory.backend, operation)
                        self._ensure(operation, issues, children)
                        return self.report
                    if self._transport is not None:
                        self._prior_launches += self._transport.launches
                        self._transport.close()
                        self._transport = None
                    if self._storage is not None:
                        self._storage.cleanup()
                    self._ready = False
                    self._theory = theory
                    # A failed new binding must not silently revive the old theory.
                    qualify_backend(theory.backend, operation)
                    theory.backend.artifacts.mkdir(parents=True, exist_ok=True)
                    self._storage = TemporaryDirectory(
                        prefix="reasoning-session-", dir=theory.backend.artifacts
                    )
                    directory = Path(self._storage.name)
                    self._request = stage_theory(theory, directory, operation)
                    self._request.update(
                        protocol=1,
                        manifest_sha256=theory.backend.manifest_sha256,
                        operation="prepare",
                        max_bytes=self._limits.max_bytes,
                        max_resources=operation.limits.max_resources,
                        max_nodes=operation.remaining("states"),
                        max_issues=operation.remaining("issues"),
                        task_timeout_ms=2**31 - 1,
                    )
                    self._preparation = encode_request(self._request, operation)
                    self._transport = FramedSession(
                        self._command(directory, operation), cap=self._limits.max_bytes
                    )
                    self._ensure(operation, issues, children)
                    return self.report
                except LimitExceeded as error:
                    issues.exhausted(str(error), location="owl.reasoning_preparation")
                except (InputError, OSError) as error:
                    self._failure(error, issues)
                failed = self._binding_failure(theory, issues, children)
                self.preparation = failed
                self._ready = False
                if self._transport is not None:
                    self._transport.reset()
                return failed.report
        except LimitExceeded as error:
            issues.exhausted(str(error), location="owl.reasoning_preparation")
        except (InputError, OSError) as error:
            self._failure(error, issues)
        # Queue failure owns no mutable session state and cannot clean up another
        # request's worker or replace its preparation receipt.
        return self._binding_failure(theory, issues, children).report

    @staticmethod
    def _binding_failure(
        theory: object, issues: IssueCollector, children: list[Report]
    ) -> SessionPreparation:
        """Construct local failure evidence independently of session ownership."""
        return SessionPreparation(
            theory.identity if type(theory) is PreparedReasoningTheory else "",
            None,
            None,
            None,
            Report(
                "owl.reasoning_preparation",
                issues=tuple(issues),
                requested=1,
                complete=False,
                children=tuple(children),
            ),
        )

    @staticmethod
    def _failure(error: InputError | OSError, issues: IssueCollector) -> None:
        """Keep unsupported resources distinct from malformed caller arguments."""
        try:
            unknown = isinstance(error, ResourceError | OSError)
            issues.append(
                Issue(
                    "owl.reasoning_session_unverified"
                    if unknown
                    else "owl.reasoning_session_input_error",
                    str(error),
                    kind="unverified" if unknown else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped), location="owl.reasoning_session")

    def _decode(self, raw: bytes, operation: Operation) -> dict[str, Any]:
        """Prepay JSON decoding workspace after bounded frame receipt."""
        if len(raw) > operation.limits.max_bytes:
            raise LimitExceeded("Reasoning session response exceeds max_bytes")
        operation.consume("work", len(raw) + 1)
        operation.consume("states", 2 * len(raw) + 8)
        operation.consume("bytes", 64 * len(raw) + 512)
        value = object_value(
            decode_json(
                raw,
                max_bytes=operation.limits.max_bytes,
                max_depth=operation.limits.max_depth,
            ),
            location="reasoning session result",
        )
        if value.get("status") == "exhausted" and set(value) <= {"protocol", "status"}:
            # Partial native answer inventories are adopted by the caller first.
            raise LimitExceeded("Native reasoning session exhausted its owned bounds")
        if value.get("status") not in {"completed", "exhausted"}:
            raise ResourceError(
                "Native reasoning session did not complete its protocol"
            )
        return value

    def _header(
        self,
        data: dict[str, Any],
        operation: Operation,
        issues: IssueCollector,
        children: list[Report],
    ) -> tuple[BackendCapability, QualifiedReceipt, Report]:
        """Reuse actual source/profile/backend qualification, never a saved verdict."""
        capability = qualify_capability(data, self.theory.backend, operation)
        children.append(capability.report)
        if result_text(data.get("theory_identity"), operation) != self.theory.identity:
            raise ResourceError("Native answer belongs to another captured theory")
        header = reasoning_header(data, operation)
        receipt = qualify_reasoning_result(
            data,
            self.theory.documents,
            self.theory.input_kind,
            operation,
            issues,
            header=header,
            scope="entailment",
        )
        if not receipt.qualified:
            if data.get("status") == "exhausted":
                raise LimitExceeded(
                    "Native theory qualification exhausted its allowance"
                )
            raise ResourceError("The selected native reasoning theory is unqualified")
        finding = None
        if receipt.raw_consistent is False:
            finding = Issue("owl.inconsistent", "The selected ontology is inconsistent")
            issues.append(finding)
        consistency = Report(
            "owl.consistency",
            int(receipt.raw_consistent is not None),
            () if finding is None else (finding,),
            requested=1,
            complete=receipt.raw_consistent is not None,
        )
        children.append(consistency)
        return capability, receipt, consistency

    def _ensure(
        self, operation: Operation, issues: IssueCollector, children: list[Report]
    ) -> None:
        """Prepare one actual process generation; failed lifetimes are never reused."""
        if self._closed.is_set():
            raise ResourceError("Reasoning session is closed")
        transport = self._transport
        if transport is None:
            raise ResourceError("Reasoning session has no prepared transport")
        if transport._process is not None and transport._process.poll() is not None:
            transport.reset()
            self._ready = False
        if self._ready:
            return
        transport._interrupted(operation.deadline, operation.cancel)
        transport._start()
        raw = transport._exchange(
            self._preparation, operation.deadline, operation.cancel, budget=operation
        )
        data = self._decode(raw, operation)
        _, receipt, _ = self._header(data, operation, issues, children)
        loads = result_counter(data.get("loads"), operation, 1)
        classifications = result_counter(data.get("classifications"), operation, 1)
        process_id = result_counter(data.get("process_id"), operation, 2**63 - 1)
        if loads != 1 or classifications != int(receipt.raw_consistent is True):
            raise ResourceError("Native preparation counters contradict qualification")
        self._classifications += classifications
        self._ready = True
        self.preparation = SessionPreparation(
            self.theory.identity,
            process_id,
            loads,
            classifications,
            Report(
                "owl.reasoning_preparation",
                1,
                tuple(issues),
                requested=1,
                children=tuple(children),
                regime="prepared-native-classification",
            ),
        )

    def _execute(
        self,
        request: dict[str, Any],
        operation: Operation,
        issues: IssueCollector,
        children: list[Report],
        expected: PreparedReasoningTheory | None = None,
    ) -> dict[str, Any]:
        """Execute once; a failed request cannot retry with undepleted allowances."""
        if expected is not None:
            if type(expected) is not PreparedReasoningTheory:
                raise InputError(
                    "Expected theory must be a captured reasoning revision"
                )
            if expected.identity != self.theory.identity:
                raise ResourceError("Expected theory differs from the current binding")
        try:
            qualify_backend(self.theory.backend, operation)
            self._ensure(operation, issues, children)
            request.update(
                protocol=1,
                manifest_sha256=self.theory.backend.manifest_sha256,
                theory_identity=self.theory.identity,
                max_bytes=min(operation.limits.max_bytes, self._limits.max_bytes),
                max_nodes=operation.remaining("states"),
                max_issues=operation.remaining("issues"),
                max_resources=operation.remaining("resources"),
                query_depth=max(1, min(64, operation.limits.max_depth // 4)),
                task_timeout_ms=max(
                    1,
                    min(2**31 - 1, int((operation.deadline - time.monotonic()) * 1000)),
                ),
            )
            payload = encode_request(request, operation)
            assert self._transport is not None
            raw = self._transport._exchange(
                payload, operation.deadline, operation.cancel, budget=operation
            )
            data = self._decode(raw, operation)
            if data["status"] != "completed" or data.get("qualified") is not True:
                # Retain the bounded partial response, but never send another
                # job into a failed or still-disposing native generation.
                self._ready = False
                self._transport.reset()
            return data
        except BaseException as error:
            self._ready = False
            if self._transport is not None:
                self._transport.reset(failure=error)
            raise
