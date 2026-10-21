"""Bounded qualification of exact composed jobs, never their bundled data."""

from collections.abc import Mapping, Sequence

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Budget, IssueCollector, retain_report
from bijux_ontology.workflows.projects.jobs.decoding import check_kind
from bijux_ontology.workflows.projects.profiles.execution import _selected_inventory
from bijux_ontology.workflows.projects.profiles.loading import _items, _text
from bijux_ontology.workflows.projects.routing.types import (
    ProfileJobBinding,
    ProfilePlan,
)

_STATIC = frozenset(("vocabulary", "models", "ontology", "skos"))


class Unavailable(InputError):
    """Selected required routing work has no supported actual-data binding."""


def plans(
    registry: Mapping[str, ProfilePlan],
    baseline: Sequence[ProfilePlan],
    budget: Budget,
) -> tuple[dict[str, ProfilePlan], tuple[ProfilePlan, ...]]:
    """Own a closed finite registry; never resolve profile identifiers remotely."""
    if not isinstance(registry, Mapping):
        raise InputError("Routing registry requires a finite local mapping")
    count = len(registry)
    budget.consume("states", count)
    owned: dict[str, ProfilePlan] = {}
    for index, (key, value) in enumerate(registry.items()):
        budget.consume("work")
        if index >= count or not isinstance(value, ProfilePlan):
            raise InputError("Routing registry changed or contains an invalid plan")
        key = _text(key, budget, iri=True)
        iri = _text(value.profile.iri, budget, iri=True)
        if key != iri or key in owned:
            raise InputError("Registry keys must identify their exact distinct plans")
        owned[key] = value
    if len(owned) != count:
        raise InputError("Routing registry changed during capture")
    required = _items(baseline, budget)
    if any(not isinstance(value, ProfilePlan) for value in required):
        raise InputError("Baselines require typed local profile plans")
    return owned, tuple(value for value in required if isinstance(value, ProfilePlan))


def bindings(
    plan: ProfilePlan, budget: Budget
) -> dict[str, dict[str, ProfileJobBinding]]:
    """Require every selected data-consuming job to bind actual caller data.

    Unsupported data jobs keep the entire plan unstarted. Running only its
    static jobs could otherwise hide required unsupported consumer obligations.
    """
    retain_report(plan.profile.report, budget, IssueCollector(budget))
    if not plan.profile.report.verified:
        raise Unavailable("Profile composition is not verified")
    _selected_inventory(plan.profile, budget)
    selected: dict[tuple[str, str], ProfileJobBinding] = {}
    for raw in _items(plan.bindings, budget):
        if not isinstance(raw, ProfileJobBinding):
            raise InputError("Expected typed profile job bindings")
        layer_name = _text(raw.layer, budget, iri=True)
        job_name = _text(raw.job, budget)
        _text(raw.data_resource, budget)
        for shape in _items(raw.shapes, budget):
            if not isinstance(shape, URIRef):
                raise InputError("Routed shape roots must be named RDF IRIs")
            _text(shape, budget, iri=True)
        key = (layer_name, job_name)
        if key in selected:
            raise InputError("Duplicate routed layer/job binding")
        selected[key] = raw
    result: dict[str, dict[str, ProfileJobBinding]] = {}
    for layer in plan.profile.layers:
        budget.consume("work")
        if not layer.selected:
            continue
        assert layer.bundle is not None and layer.profile is not None
        applied: dict[str, set[str]] = {}
        for job in layer.bundle.configuration.checks():
            budget.consume("work")
            name = _text(job.get("name"), budget)
            kind = check_kind(job)
            binding = selected.pop((layer.iri, name), None)
            if kind in _STATIC:
                if binding is not None:
                    raise Unavailable("Static jobs cannot consume routed data bindings")
                continue
            if kind != "shacl" or binding is None:
                raise Unavailable(
                    "Every selected data job requires a supported SHACL binding"
                )
            document = _text(job.get("document"), budget)
            source = _text(job.get("shapes"), budget)
            roots = tuple(
                _text(value, budget, iri=True)
                for value in _items(job.get("use_shapes", ()), budget)
            )
            actual = tuple(
                _text(value, budget, iri=True)
                for value in _items(binding.shapes, budget)
            )
            if (
                not roots
                or len(set(roots)) != len(roots)
                or set(actual) != set(roots)
                or binding.data_resource != document
            ):
                raise Unavailable(
                    "Routing must retain the actual data alias and complete job roots"
                )
            budget.consume("states", len(roots))
            applied.setdefault(source, set()).update(roots)
            budget.consume("states")
            result.setdefault(layer.iri, {})[name] = binding
        declared = {}
        for inventory in _items(layer.profile.shapes, budget):
            from bijux_ontology.workflows.projects.profiles.types import ProfileShapes

            if not isinstance(inventory, ProfileShapes):
                raise InputError("Expected typed profile root inventories")
            alias = _text(inventory.source, budget)
            declared[alias] = {
                _text(value, budget, iri=True)
                for value in _items(inventory.roots, budget)
            }
        if applied != declared:
            raise Unavailable(
                "Routing roots differ from their complete profile inventory"
            )
    if selected:
        raise Unavailable("Routing bindings select unavailable or unselected jobs")
    if not result:
        raise Unavailable("A routed plan requires an actual selected data job")
    return result
