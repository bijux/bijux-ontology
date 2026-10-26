"""Jena capability policy beside core-owned native SHACL role preparation."""

from collections.abc import Mapping, Sequence

from rdflib import Graph

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.sources.types import Payload, RdfSource
from bijux_ontology.shacl.native.planning import NativeShaclPlan, prepare_native_plan
from bijux_ontology.shacl.targets.coverage import ShaclCoverageContract
from bijux_ontology.shacl.validation.bindings import TermSelection
from bijux_ontology.shacl.validation.contracts import ShaclConstraintRequirement


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
) -> NativeShaclPlan:
    """Declare the supported Jena profile without invoking another SHACL engine."""
    if any(
        type(value) is not bool for value in (trusted_sparql, preflight, allow_empty)
    ):
        raise InputError("Jena SHACL flags require exact booleans")
    return prepare_native_plan(
        shapes,
        ontology,
        imports,
        use_shapes,
        mode,
        trusted_sparql,
        preflight,
        allow_empty,
        coverage,
        requirements,
        budget,
        preflight_finding=None
        if not preflight
        else Issue(
            "shacl.jena_capability_unavailable",
            "Meta-SHACL preflight is outside this Jena execution profile",
            kind="unverified",
        ),
    )
