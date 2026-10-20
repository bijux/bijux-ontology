"""Reuse owned SHACL role/selection contracts without Python-engine discovery."""

from collections.abc import Mapping, Sequence

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.bindings import TermSelection, resolve_terms
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.execution.planning import _role, _ShapePlan
from bijux_ontology.shacl.execution.session import _contract_metadata, _prepay_graph
from bijux_ontology.shacl.functions import FunctionPlan


def prepare_plan(
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
) -> _ShapePlan:
    """Capture the same public contract; native Jena discovers actual capabilities."""
    if any(
        type(value) is not bool for value in (trusted_sparql, preflight, allow_empty)
    ):
        raise InputError("Jena SHACL flags require exact booleans")
    if type(mode) is not str:
        raise InputError("SHACL mode requires plain text")
    bound_text(mode, budget)
    if mode not in {"targets", "pairs"}:
        raise InputError("SHACL mode must be targets or pairs")
    selected = _contract_metadata(coverage, requirements, None, budget)
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
            _prepay_graph(
                source._read(budget=budget)
                if isinstance(source, PreparedSource)
                else source,
                budget,
            )
    sg = _role(
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
        else _role(
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
    features = (
        ()
        if not preflight
        else (
            Issue(
                "shacl.jena_capability_unavailable",
                "Meta-SHACL preflight is outside this Jena execution profile",
                kind="unverified",
            ),
        )
    )
    return _ShapePlan(
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
    )
