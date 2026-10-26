"""Owned immutable Python SHACL roles, bindings and fixed native preparation."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.budgets import operation
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.sources.loading import _charge_graph
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.execution.planning import _prepare_plan
from bijux_ontology.shacl.sparql.functions import ShaclFunctionCatalog
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.bindings import (
    StaticBindings,
    TermSelection,
    transport_roles,
)
from bijux_ontology.shacl.validation.contracts import (
    ShaclConstraintRequirement,
    ShaclPlan,
    capture_contract,
    request_options,
)
from bijux_ontology.shacl.validation.encoding import encode_roles
from bijux_ontology.shacl.validation.roles import prepay_graph, role_graphs


@dataclass(frozen=True, slots=True, init=False)
class SessionPreparation:
    """Own captured roles and fixed options independently of request grants."""

    plan: ShaclPlan
    bindings: StaticBindings
    payload: bytes

    def __init__(self) -> None:
        raise InputError("Prepare fixed SHACL roles through prepare_session")

    @classmethod
    def _owned(
        cls, plan: ShaclPlan, bindings: StaticBindings, payload: bytes
    ) -> "SessionPreparation":
        result = object.__new__(cls)
        object.__setattr__(result, "plan", plan)
        object.__setattr__(result, "bindings", bindings)
        object.__setattr__(result, "payload", payload)
        return result


def preparation_budget(
    limits: Limits | None, budget: Budget | None, memory_bytes: int | None
) -> Budget:
    """Qualify fixed preparation options before selecting one cumulative grant."""
    if memory_bytes is not None and (
        type(memory_bytes) is not int or memory_bytes <= 0
    ):
        raise InputError("memory_bytes must be a positive integer")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("SHACL session requires Limits")
    return operation(limits, budget)


def prepare_session(
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
    memory_bytes: int | None = None,
) -> SessionPreparation:
    """Capture fixed roles once, without retaining a live worker or request deadline."""
    storage = preparation_budget(limits, budget, memory_bytes)
    required_constraints = capture_contract(
        coverage, required_constraints, functions, storage
    )
    for source in (shapes, ontology):
        if source is not None:
            if not isinstance(source, Graph | PreparedSource):
                raise InputError("SHACL roles require graphs or prepared sources")
            prepay_graph(
                source._read(budget=storage)
                if isinstance(source, PreparedSource)
                else source,
                storage,
            )
    if functions is not None:
        if not isinstance(functions, ShaclFunctionCatalog):
            raise InputError("functions requires a ShaclFunctionCatalog")
        prepay_graph(functions._graph, storage)
    storage.consume("bytes", 1024)
    storage.consume("states", 16)
    plan = _prepare_plan(
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
        limits=storage.limits,
        budget=storage,
    )
    storage.consume("states", 1)
    storage.consume("bytes", 512)
    plan = replace(plan, budget=None)
    for graph in (
        plan.shapes,
        plan.ontology,
        plan.function_plan.graph if plan.has_functions else None,
    ):
        if graph is not None:
            storage.consume("resources")
            _charge_graph(graph, storage, plan.limits.max_bytes)
    prepay_graph(plan.shapes, storage)
    roles = role_graphs(plan)
    bindings = StaticBindings(roles, plan.limits, budget=storage)
    static = transport_roles(
        roles,
        (),
        plan.roots,
        plan.limits,
        anchors=bindings.anchors,
        budget=storage,
    )
    preparation = encode_roles(
        static,
        {
            "command": "prepare",
            **request_options(plan, ()),
            "anchor_count": len(bindings.anchors),
            "memory_bytes": memory_bytes,
        },
        storage,
    )
    return SessionPreparation._owned(plan, bindings, preparation)
