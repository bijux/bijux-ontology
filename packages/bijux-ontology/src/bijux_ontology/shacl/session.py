"""Owned fixed SHACL contracts with bounded reusable isolated execution."""

import json
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from types import TracebackType
from typing import cast

from rdflib import Graph

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import _charge_graph
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.reports import Issue, Report, combine_reports
from bijux_ontology.shacl.bindings import StaticBindings, TermSelection, transport_roles
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.checking import (
    ShaclResult,
    _incomplete,
    _metadata,
    _options,
    _prepare_data,
    _prepare_plan,
    _project_result,
    _role_graphs,
)
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.findings import ShaclSource
from bijux_ontology.shacl.functions import ShaclFunctionCatalog
from bijux_ontology.shacl.transport import SessionBackend


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
    The first job includes worker startup; subsequent jobs reuse parsed roles.
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
        use_shapes: Sequence[TermSelection] = (),
        mode: str = "targets",
        preflight: bool = False,
        allow_empty: bool = False,
        coverage: ShaclCoverageContract | None = None,
        required_constraints: Sequence[ShaclConstraintRequirement] = (),
        functions: ShaclFunctionCatalog | None = None,
        limits: Limits | None = None,
        timeout: float | None = 30.0,
        memory_bytes: int | None = None,
    ) -> None:
        if timeout is not None and (
            type(timeout) not in (int, float) or not isfinite(timeout) or timeout <= 0
        ):
            raise InputError("timeout must be a positive finite number or None")
        if memory_bytes is not None and (
            type(memory_bytes) is not int or memory_bytes <= 0
        ):
            raise InputError("memory_bytes must be a positive integer")
        self._plan = _prepare_plan(
            shapes,
            ontology=ontology,
            imports=imports,
            trusted_sparql=trusted_sparql,
            use_shapes=use_shapes,
            mode=mode,
            preflight=preflight,
            allow_empty=allow_empty,
            coverage=coverage,
            required_constraints=required_constraints,
            functions=functions,
            limits=limits if limits is not None else Limits(),
        )
        storage = Budget(self._plan.limits)
        for graph in (
            self._plan.shapes,
            self._plan.ontology,
            self._plan.function_plan.graph if self._plan.has_functions else None,
        ):
            if graph is not None:
                storage.consume("resources")
                _charge_graph(graph, storage, self._plan.limits.max_bytes)
        roles = _role_graphs(self._plan)
        self._bindings = StaticBindings(roles, self._plan.limits)
        static = transport_roles(
            roles,
            (),
            self._plan.roots,
            self._plan.limits,
            anchors=self._bindings.anchors,
        )
        preparation = json.dumps(
            {
                "command": "prepare",
                "graphs": static.serialize(format="nquads"),
                **_options(self._plan, ()),
                "anchor_count": len(self._bindings.anchors),
                "memory_bytes": memory_bytes,
            }
        ).encode()
        if len(preparation) > self._plan.limits.max_total_bytes:
            raise LimitExceeded("Prepared SHACL roles exceed max_total_bytes")
        self._backend = SessionBackend(
            preparation, cap=self._plan.limits.max_total_bytes
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
    ) -> ShaclResult:
        if self._closed.is_set():
            raise InputError("SHACL session is closed")
        if scope not in ("dataset", "records"):
            raise InputError("SHACL scope must be dataset or records")
        if cancel is not None and not isinstance(cancel, threading.Event):
            raise InputError("cancel requires a threading.Event")
        if len(source_usages) > self._plan.limits.max_resources:
            raise LimitExceeded("SHACL source usages exceed max_resources")
        if any(not isinstance(source, ShaclSource) for source in source_usages):
            raise InputError("Expected explicit prepared SHACL source usages")
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        # Waiting callers own no worker computation and must not reset its state.
        try:
            while not self._lock.acquire(timeout=0.01):
                self._backend._interrupted(deadline, cancel)
        except (LimitExceeded, ResourceError) as error:
            return _incomplete(self._plan, error)
        try:
            self._backend._interrupted(deadline, cancel)
            dg, focus = _prepare_data(
                data,
                self._plan,
                focus_nodes,
                graph_name=graph_name,
                union=union,
            )
            if self._plan.features:
                return ShaclResult(
                    Report(
                        "shacl",
                        0,
                        self._plan.features,
                        requested=1,
                        complete=False,
                        families=self._plan.families,
                    ),
                    **_metadata(self._plan, focus),
                )
            references = self._bindings.references(dg)
            roles = transport_roles(
                {"data": dg, "anchors": references}, focus, (), self._plan.limits
            )
            payload = json.dumps(
                {
                    "command": "validate",
                    "graphs": roles.serialize(format="nquads"),
                    "focus_nodes": tuple(str(node) for node in focus)
                    if self._plan.mode == "targets"
                    else (),
                    "binding_count": len(focus),
                    "anchor_count": len(references),
                }
            ).encode()
            if budget is not None:
                budget.consume("bytes", len(payload))
            raw = self._backend.execute(payload, deadline=deadline, cancel=cancel)
            if budget is not None:
                budget.consume("bytes", len(raw))
            try:
                result = _project_result(
                    raw,
                    self._plan,
                    data,
                    dg,
                    focus,
                    graph_name=graph_name,
                    union=union,
                    source_usages=source_usages,
                )
            except BaseException:
                self._backend.reset()
                raise
            if result.report.outcome in {"input_error", "exhausted"}:
                self._backend.reset()
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
            self._backend._interrupted(deadline, cancel)
            return result
        except (InputError, ResourceError) as error:
            if isinstance(error, LimitExceeded):
                self._backend.reset()
            return _incomplete(self._plan, error)
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
        try:
            selected = tuple(datasets[index] for index in range(capacity))
        except Exception as error:
            raise InputError(
                "Cannot capture the selected SHACL dataset inventory"
            ) from error
        budget = Budget(self._plan.limits)
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
                budget.consume("issues", len(result.report.issues))
                budget.consume("states", len(result.findings))
            except LimitExceeded as error:
                result = _incomplete(self._plan, error)
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
