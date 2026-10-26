"""Owned fixed SHACL contracts with bounded reusable isolated execution."""

import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from math import isfinite
from types import TracebackType
from typing import cast

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.budgets import Operation, operation
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.evidence.findings import ShaclSource
from bijux_ontology.shacl.evidence.results import ShaclResult
from bijux_ontology.shacl.execution.preparation import (
    SessionPreparation,
    preparation_budget,
    prepare_session,
)
from bijux_ontology.shacl.execution.transport import SessionBackend
from bijux_ontology.shacl.sparql.functions import ShaclFunctionCatalog
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.adoption import (
    adopt_result,
    incomplete_result,
    retain_result,
)
from bijux_ontology.shacl.validation.bindings import (
    TermSelection,
    transport_roles,
)
from bijux_ontology.shacl.validation.contracts import (
    ShaclConstraintRequirement,
    capture_contract,
    selection_metadata,
)
from bijux_ontology.shacl.validation.encoding import encode_roles
from bijux_ontology.shacl.validation.roles import (
    prepare_data,
    prepay_graph,
)


@dataclass(frozen=True)
class ShaclBatchResult:
    """Retain bounded per-dataset raw results and every required pending scope."""

    results: tuple[ShaclResult, ...]
    report: Report


class ShaclSession:
    """Own one immutable authored-shape/ontology/options contract and worker.

    Construct another session to replace resources or fixed options. Original
    Paths, graphs and catalogs are not consulted again. Each validation owns its
    data selection. No validation state or function registration crosses jobs.
    Independently trusted SPARQL targets are enumerated for each fresh job;
    their results never replace the fixed authored targeting contract.
    The first job includes worker startup; subsequent jobs reuse parsed roles.
    An explicit budget is cumulative across construction and every call; omitted
    budgets retain independent calls, with one cumulative budget per batch.
    Deadlines include queueing, startup and backend execution. Cancellation while
    queueing does not stop another request. Close cancels active work and reaps
    the owned process group; use a context manager or explicit close.
    """

    def __init__(
        self,
        shapes: Graph | PreparedSource,
        *,
        ontology: Graph | PreparedSource | None = None,
        imports: Mapping[str, RdfSource | Payload] | None = None,
        trusted_sparql: bool = False,
        trusted_targets: bool = False,
        use_shapes: Sequence[TermSelection] = (),
        mode: str = "targets",
        preflight: bool = False,
        allow_empty: bool = False,
        coverage: ShaclCoverageContract | None = None,
        required_constraints: Sequence[ShaclConstraintRequirement] = (),
        functions: ShaclFunctionCatalog | None = None,
        limits: Limits | None = None,
        budget: Budget | None = None,
        timeout: float | None = 30.0,
        memory_bytes: int | None = None,
    ) -> None:
        if timeout is not None and (
            type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0
        ):
            raise InputError("timeout must be a positive finite number or None")
        storage = preparation_budget(limits, budget, memory_bytes)
        preparation = prepare_session(
            shapes,
            ontology=ontology,
            imports=imports,
            trusted_sparql=trusted_sparql,
            trusted_targets=trusted_targets,
            use_shapes=use_shapes,
            mode=mode,
            preflight=preflight,
            allow_empty=allow_empty,
            coverage=coverage,
            required_constraints=required_constraints,
            functions=functions,
            limits=limits,
            budget=storage,
            memory_bytes=memory_bytes,
        )
        self._reserve_runtime(storage)
        self._initialize(preparation, budget, timeout)

    @classmethod
    def _from_preparation(
        cls,
        preparation: SessionPreparation,
        *,
        storage: Budget,
    ) -> "ShaclSession":
        """Create one lazy worker lifetime from exact revision-owned preparation."""
        if type(preparation) is not SessionPreparation:
            raise InputError("Expected owned fixed SHACL preparation")
        operation(preparation.plan.limits, storage)
        storage.consume("work", 0)
        cls._reserve_runtime(storage)
        result = object.__new__(cls)
        result._initialize(preparation, None, None)
        return result

    @staticmethod
    def _reserve_runtime(storage: Budget) -> None:
        """Fund controlled session/backend, commands, locks, events and conditions."""
        # Fixed host owners exclude paid roles/payload and native process memory.
        storage.consume("bytes", 8192)
        storage.consume("states", 16)

    def _initialize(
        self,
        preparation: SessionPreparation,
        budget: Budget | None,
        timeout: float | None,
    ) -> None:
        """Initialize a paid runtime without re-reading fixed semantic resources."""
        self._budget = budget
        self._plan = (
            preparation.plan
            if budget is None
            else replace(preparation.plan, budget=budget)
        )
        self._bindings = preparation.bindings
        self._backend = SessionBackend(
            preparation.payload, cap=self._plan.limits.max_total_bytes
        )
        self._timeout = timeout
        self._lock = threading.Lock()
        self._closed = threading.Event()

    @property
    def worker_launches(self) -> int:
        """Count actual launches, including fresh workers after failed batches."""
        return self._backend.launches

    def close(self) -> None:
        """Cancel active work, reap its process group and reject subsequent jobs."""
        self._closed.set()
        self._backend.close()

    def __enter__(self) -> "ShaclSession":
        if self._closed.is_set():
            raise InputError("SHACL session is closed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def validate(
        self,
        data: Graph | PreparedSource,
        *,
        focus_nodes: Sequence[TermSelection] = (),
        graph_name: GraphSelection | None = None,
        union: bool = False,
        source_usages: Sequence[ShaclSource] = (),
        scope: str = "dataset",
        cancel: threading.Event | None = None,
    ) -> ShaclResult:
        """Validate one exact dataset; record-local scope retains global unknowns."""
        return self._validate(
            data,
            focus_nodes=focus_nodes,
            graph_name=graph_name,
            union=union,
            source_usages=source_usages,
            scope=scope,
            cancel=cancel,
            budget=None,
        )

    def _validate(
        self,
        data: Graph | PreparedSource,
        *,
        focus_nodes: Sequence[TermSelection],
        graph_name: GraphSelection | None,
        union: bool,
        source_usages: Sequence[ShaclSource],
        scope: str,
        cancel: threading.Event | None,
        budget: Budget | None,
        coverage: ShaclCoverageContract | None = None,
    ) -> ShaclResult:
        if self._closed.is_set():
            raise InputError("SHACL session is closed")
        if (
            budget is not None
            and self._budget is not None
            and budget is not self._budget
        ):
            raise InputError("Use the session-owned cumulative Budget")
        chosen = operation(
            self._plan.limits, self._budget if budget is None else budget
        )
        if coverage is not None:
            if self._plan.coverage is None:
                raise InputError("Fixed SHACL preparation did not enable coverage")
            capture_contract(coverage, (), None, chosen)
        plan = replace(
            self._plan,
            budget=chosen,
            coverage=self._plan.coverage if coverage is None else coverage,
        )
        if scope not in ("dataset", "records"):
            raise InputError("SHACL scope must be dataset or records")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise InputError("cancel requires a threading.Event")
        if len(source_usages) > self._plan.limits.max_resources:
            raise LimitExceeded("SHACL source usages exceed max_resources")
        if any(not isinstance(source, ShaclSource) for source in source_usages):
            raise InputError("Expected explicit prepared SHACL source usages")
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        if isinstance(chosen, Operation):
            if (
                cancel is not None
                and chosen.cancel is not None
                and cancel is not chosen.cancel
            ):
                raise InputError("Nested SHACL cancellation requires the same Event")
            if cancel is None:
                cancel = chosen.cancel
            deadline = (
                chosen.deadline if deadline is None else min(deadline, chosen.deadline)
            )
        # Waiting callers own no worker computation and must not reset its state.
        try:
            while not self._lock.acquire(timeout=0.01):
                self._backend.checkpoint(deadline, cancel)
        except (LimitExceeded, ResourceError) as error:
            return incomplete_result(plan, error)
        try:
            self._backend.checkpoint(deadline, cancel)
            source_usages = capture(source_usages, chosen)
            graph = (
                data._read(graph_name, union=union, budget=chosen)
                if isinstance(data, PreparedSource)
                else data
            )
            if not isinstance(graph, Graph):
                raise InputError("SHACL data requires a graph or prepared source")
            prepay_graph(graph, chosen)
            dg, focus = prepare_data(
                data,
                plan,
                focus_nodes,
                graph_name=graph_name,
                union=union,
            )
            if plan.features:
                return retain_result(
                    ShaclResult(
                        Report(
                            "shacl",
                            0,
                            plan.features,
                            requested=1,
                            complete=False,
                            families=plan.families,
                        ),
                        **selection_metadata(plan, focus),
                    ),
                    chosen,
                )
            references = self._bindings.references(dg, budget=chosen)
            roles = transport_roles(
                {"data": dg, "anchors": references},
                focus,
                (),
                plan.limits,
                budget=chosen,
            )
            chosen.consume("states", 8 + len(focus))
            chosen.consume("bytes", 512 + 64 * len(focus))
            payload = encode_roles(
                roles,
                {
                    "command": "validate",
                    "focus_nodes": tuple(str(node) for node in focus)
                    if plan.mode == "targets"
                    else (),
                    "binding_count": len(focus),
                    "anchor_count": len(references),
                },
                chosen,
            )
            raw = self._backend.execute(
                payload, deadline=deadline, cancel=cancel, budget=chosen
            )
            chosen.consume("work", len(raw) + 1)
            chosen.consume("states", 2 * len(raw) + 16)
            chosen.consume("bytes", 64 * len(raw) + 1024)
            try:
                result = adopt_result(
                    raw,
                    plan,
                    data,
                    dg,
                    focus,
                    graph_name=graph_name,
                    union=union,
                    source_usages=source_usages,
                )
            except BaseException as error:
                self._backend.reset(failure=error)
                raise
            if result.report.outcome in {"input_error", "exhausted"}:
                try:
                    self._backend.reset()
                except ResourceError as error:
                    result.report = replace(
                        result.report,
                        issues=result.report.issues
                        + (
                            Issue(
                                "shacl.execution_incomplete",
                                str(error),
                                kind="unverified",
                            ),
                        ),
                        complete=False,
                    )
            if scope == "records":
                result.report = combine_reports(
                    "shacl",
                    result.report,
                    Report(
                        "shacl-dataset-scope",
                        requested=1,
                        complete=False,
                        issues=(
                            Issue(
                                "shacl.dataset_scope_unverified",
                                "Record-local data does not establish conformance "
                                "over the wider dataset",
                                kind="unverified",
                            ),
                        ),
                    ),
                )
            try:
                self._backend.checkpoint(deadline, cancel)
            except (LimitExceeded, ResourceError) as error:
                result.report = replace(
                    result.report,
                    issues=result.report.issues
                    + (
                        Issue(
                            "shacl.execution_incomplete",
                            str(error),
                            kind="exhausted"
                            if isinstance(error, LimitExceeded)
                            else "unverified",
                        ),
                    ),
                    complete=False,
                )
            return retain_result(result, chosen)
        except (InputError, ResourceError) as error:
            if isinstance(error, LimitExceeded):
                self._backend.reset(failure=error)
            return incomplete_result(plan, error)
        finally:
            self._lock.release()

    def validate_batch(
        self,
        datasets: Sequence[Graph | PreparedSource],
        *,
        scope: str = "dataset",
        cancel: threading.Event | None = None,
    ) -> ShaclBatchResult:
        """Bound cumulative retention and identify required pending datasets."""
        if not isinstance(datasets, Sequence) or isinstance(
            cast(object, datasets), (str, bytes)
        ):
            raise InputError("SHACL batches require a finite dataset sequence")
        required = len(datasets)
        if not required:
            raise InputError("SHACL batch must select at least one dataset")
        # Freeze only the bounded execution window; retain the full required tail.
        capacity = min(required, self._plan.limits.max_resources + 1)
        budget = operation(self._plan.limits, self._budget)
        try:
            budget.consume("states", capacity * 3)
            budget.consume("bytes", 128 + capacity * 128)
            selected = tuple(datasets[index] for index in range(capacity))
        except LimitExceeded as error:
            return ShaclBatchResult(
                (),
                replace(
                    incomplete_result(self._plan, error).report, requested=required
                ),
            )
        except Exception as error:
            raise InputError(
                "Cannot capture the selected SHACL dataset inventory"
            ) from error
        results = []
        reports = []
        for index, data in enumerate(selected):
            try:
                budget.consume("resources")
                result = self._validate(
                    data,
                    focus_nodes=(),
                    graph_name=None,
                    union=False,
                    source_usages=(),
                    scope=scope,
                    cancel=cancel,
                    budget=budget,
                )
            except LimitExceeded as error:
                result = incomplete_result(self._plan, error)
            results.append(result)
            reports.append(combine_reports(f"shacl-batch/{index}", result.report))
            if result.report.outcome == "exhausted":
                break
        pending = required - len(results)
        if pending:
            reports.append(
                Report(
                    "shacl-batch-pending",
                    requested=pending,
                    complete=False,
                    issues=(
                        Issue(
                            "shacl.execution_incomplete",
                            f"{pending} required datasets were not started",
                            kind="exhausted",
                        ),
                    ),
                )
            )
        return ShaclBatchResult(
            tuple(results), combine_reports("shacl-batch", *reports)
        )
