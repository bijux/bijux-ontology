"""Capture native validator shape roles without invoking a Python SHACL engine."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rdflib import BNode, Graph, Literal, URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.sparql.functions import FunctionPlan
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.bindings import TermSelection, resolve_terms
from bijux_ontology.shacl.validation.contracts import (
    ShaclConstraintRequirement,
    ShaclPlan,
    capture_contract,
)
from bijux_ontology.shacl.validation.roles import prepare_role, prepay_graph


@dataclass(frozen=True, init=False)
class NativeShaclPlan:
    """Factory-owned authored shape roles and the original active allowance."""

    _value: ShaclPlan
    _trusted_sparql: bool

    def __init__(self) -> None:
        """Require captured shape roles rather than caller-built native plans."""
        raise InputError("Use prepare_native_plan to capture native SHACL roles")

    @classmethod
    def _owned(cls, value: ShaclPlan, trusted_sparql: bool) -> "NativeShaclPlan":
        """Retain the already funded role and selection contract."""
        result = object.__new__(cls)
        object.__setattr__(result, "_value", value)
        object.__setattr__(result, "_trusted_sparql", trusted_sparql)
        return result

    @property
    def features(self) -> tuple[Issue, ...]:
        """Retain backend capability limitations independently of conformance."""
        return self._value.features

    @property
    def mode(self) -> str:
        """Expose the captured target or explicit-pair selection mode."""
        return self._value.mode

    @property
    def roots(self) -> tuple[URIRef | BNode | Literal, ...]:
        """Expose only the owned authored shape selections."""
        return self._value.roots

    @property
    def preflight(self) -> bool:
        """Retain the caller's meta-validation request even when unsupported."""
        return self._value.preflight


def prepare_native_plan(
    shapes: Graph | PreparedSource,
    ontology: Graph | PreparedSource | None,
    imports: Mapping[str, RdfSource | Payload] | None,
    use_shapes: Sequence[TermSelection],
    mode: str,
    trusted_sparql: bool,
    preflight: bool,
    allow_empty: bool,
    coverage: ShaclCoverageContract | None,
    requirements: Sequence[ShaclConstraintRequirement],
    budget: Budget,
    *,
    preflight_finding: Issue | None = None,
) -> NativeShaclPlan:
    """Capture supplied roles and selections under one native-check allowance.

    The captured trust flag fixes the Core/SELECT family selection. The adapter
    supplies any preflight limitation. Target and constraint discovery remain native;
    no Python engine is loaded and no backend capability is inferred here.
    """
    if any(
        type(value) is not bool for value in (trusted_sparql, preflight, allow_empty)
    ):
        raise InputError("Native SHACL flags require exact booleans")
    if preflight_finding is not None and type(preflight_finding) is not Issue:
        raise InputError("Expected an explicit native SHACL preflight finding")
    if type(mode) is not str:
        raise InputError("SHACL mode requires plain text")
    bound_text(mode, budget)
    if mode not in {"targets", "pairs"}:
        raise InputError("SHACL mode must be targets or pairs")
    selected = capture_contract(coverage, requirements, None, budget)
    budget.consume("bytes", 512 + 256 * len(selected))
    owned_requirements = tuple(
        ShaclConstraintRequirement(item.shape, item.parameter, item.component)
        for item in selected
    )
    owned_coverage = None
    if coverage is not None:
        owned_coverage = ShaclCoverageContract(
            coverage.minimum_focus, tuple(coverage.focus_nodes), tuple(coverage.pairs)
        )
    for source in (shapes, ontology):
        if source is not None:
            if not isinstance(source, Graph | PreparedSource):
                raise InputError("SHACL roles require graphs or prepared sources")
            prepay_graph(
                source._read(budget=budget)
                if isinstance(source, PreparedSource)
                else source,
                budget,
            )
    sg = prepare_role(
        shapes,
        graph_name=None,
        union=False,
        limits=budget.limits,
        imports=imports,
        resolve_imports=True,
        budget=budget,
    )
    og = (
        None
        if ontology is None
        else prepare_role(
            ontology,
            graph_name=None,
            union=False,
            limits=budget.limits,
            imports=imports,
            resolve_imports=True,
            budget=budget,
        )
    )
    roots = resolve_terms(
        use_shapes,
        shapes,
        sg,
        shapes=True,
        limits=budget.limits,
        budget=budget,
    )
    if mode == "pairs" and not roots:
        raise InputError("Pairs mode requires selected shapes and focus nodes")
    for root in roots:
        if (root, None, None) not in sg and (None, None, root) not in sg:
            raise InputError("Selected shape is absent", location=str(root))
    budget.consume("bytes", 1024)
    features = () if preflight_finding is None else (preflight_finding,)
    return NativeShaclPlan._owned(
        ShaclPlan(
            sg,
            og,
            roots,
            FunctionPlan(Graph()),
            False,
            features,
            ("shacl-core", *(("shacl-sparql",) if trusted_sparql else ())),
            budget.limits,
            mode,
            preflight,
            allow_empty,
            owned_coverage,
            owned_requirements,
            False,
            budget,
        ),
        trusted_sparql,
    )
