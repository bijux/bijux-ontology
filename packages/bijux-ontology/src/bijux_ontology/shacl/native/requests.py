"""Capture native SHACL data, owned selections and bounded transport together."""

from collections.abc import Sequence
from dataclasses import dataclass

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.json.paths import bound_index
from bijux_ontology.rdf.sources.snapshots import GraphSelection, PreparedSource
from bijux_ontology.shacl.evidence.findings import ShaclSource
from bijux_ontology.shacl.validation.bindings import TermSelection, transport_roles
from bijux_ontology.shacl.validation.contracts import request_options
from bijux_ontology.shacl.validation.encoding import encode_roles
from bijux_ontology.shacl.validation.roles import (
    prepare_data,
    prepay_graph,
    role_graphs,
)

from .planning import NativeShaclPlan


@dataclass(frozen=True, init=False)
class NativeShaclRequest:
    """One captured data role, source provenance and original operation owner."""

    _plan: NativeShaclPlan
    _data: Graph
    _focus: tuple[URIRef | BNode | Literal, ...]
    _graph_name: GraphSelection | None
    _union: bool
    _sources: tuple[ShaclSource, ...]
    _budget: Budget

    def __init__(self) -> None:
        """Require paid source and term capture before native execution."""
        raise InputError("Use prepare_native_request to capture native SHACL data")

    @classmethod
    def _owned(
        cls,
        plan: NativeShaclPlan,
        data: Graph,
        focus: tuple[URIRef | BNode | Literal, ...],
        graph_name: GraphSelection | None,
        union: bool,
        sources: tuple[ShaclSource, ...],
        budget: Budget,
    ) -> "NativeShaclRequest":
        """Retain only captured roles within their existing storage reservation."""
        result = object.__new__(cls)
        for name, value in (
            ("_plan", plan),
            ("_data", data),
            ("_focus", focus),
            ("_graph_name", graph_name),
            ("_union", union),
            ("_sources", sources),
            ("_budget", budget),
        ):
            object.__setattr__(result, name, value)
        return result

    @property
    def focus(self) -> tuple[URIRef | BNode | Literal, ...]:
        """Expose original resolved terms without relabelling blank identities."""
        return self._focus


def prepare_native_request(
    data: Graph | PreparedSource,
    plan: NativeShaclPlan,
    focus_nodes: Sequence[TermSelection],
    *,
    graph_name: GraphSelection | None = None,
    union: bool = False,
) -> NativeShaclRequest:
    """Capture data and source paths on the shape plan's same paid allowance."""
    if type(plan) is not NativeShaclPlan or plan._value.budget is None:
        raise InputError("Expected a prepared native SHACL plan with its allowance")
    if type(union) is not bool:
        raise InputError("Dataset union selection requires an exact boolean")
    if not isinstance(data, Graph | PreparedSource):
        raise InputError("SHACL data requires a graph or prepared source")
    operation = plan._value.budget
    original_graph, original_union = graph_name, union
    if isinstance(data, PreparedSource) and (data.graph_name is not None or data.union):
        original_graph, original_union = data.graph_name, data.union
    prepay_graph(
        data._read(graph_name, union=union, budget=operation)
        if isinstance(data, PreparedSource)
        else data,
        operation,
    )
    dg, focus = prepare_data(
        data,
        plan._value,
        focus_nodes,
        graph_name=graph_name,
        union=union,
    )
    return NativeShaclRequest._owned(
        plan, dg, focus, original_graph, original_union, (), operation
    )


def capture_native_sources(
    request: NativeShaclRequest, source_usages: Sequence[ShaclSource]
) -> NativeShaclRequest:
    """Attach paid source paths while retaining already resolved focus evidence."""
    if type(request) is not NativeShaclRequest:
        raise InputError("Expected a prepared native SHACL request")
    operation = request._budget
    supplied_sources = capture(source_usages, operation)
    operation.consume("bytes", 512 + 256 * len(supplied_sources))
    owned_sources: list[ShaclSource] = []
    for source in supplied_sources:
        if type(source) is not ShaclSource:
            raise InputError("Expected ordinary prepared SHACL source usages")
        prepared, paths, label = source.prepared, source.paths, source.source
        bound_text(label, operation)
        owned_paths: list[tuple[str | int, ...]] = []
        for path in capture(paths, operation):
            members: list[str | int] = []
            for member in capture(path, operation):
                if type(member) is str:
                    bound_text(member, operation)
                elif type(member) is int:
                    bound_index(member, operation)
                else:
                    raise InputError("Source paths require string/integer members")
                members.append(member)
            owned_paths.append(tuple(members))
        owned_sources.append(ShaclSource(prepared, tuple(owned_paths), label))
    return NativeShaclRequest._owned(
        request._plan,
        request._data,
        request._focus,
        request._graph_name,
        request._union,
        tuple(owned_sources),
        operation,
    )


def encode_native_request(request: NativeShaclRequest) -> bytes:
    """Encode the fixed native-validator protocol without reopening input sources."""
    if type(request) is not NativeShaclRequest:
        raise InputError("Expected an owned native SHACL request")
    operation, plan = request._budget, request._plan._value
    operation.consume("bytes", 1024)
    roles = transport_roles(
        {"data": request._data, **role_graphs(plan)},
        request._focus,
        plan.roots,
        operation.limits,
        budget=operation,
    )
    options = request_options(plan, request._focus)
    options.update(
        protocol=1,
        operation="shacl",
        trusted_sparql=request._plan._trusted_sparql,
        required_constraints=[
            {
                "shape": item.shape,
                "parameter": item.parameter,
                "component": item.component,
            }
            for item in plan.required_constraints
        ],
    )
    return encode_roles(roles, options, operation)
