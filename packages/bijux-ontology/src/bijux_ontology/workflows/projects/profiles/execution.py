"""Explicit owned materialization followed by the ordinary project contract runner."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from bijux_ontology._json import object_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.reports import AcceptancePolicy, Issue, Report
from bijux_ontology.workflows.projects.checking import check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    ContractResult,
    check_contract,
)
from bijux_ontology.workflows.projects.inputs import ShaclJobInput, capture_job_inputs

from .identity import digest, execution_digest, layer_key
from .loading import _items, _qualify_profile, _text
from .types import ProfileDecision, ProfileLayer


@dataclass(frozen=True)
class EffectiveProfile:
    """Deterministic layer inventory; successful composition is not conformance."""

    iri: str
    layers: tuple[ProfileLayer, ...]
    decisions: tuple[ProfileDecision, ...]
    report: Report
    limits: Limits

    def check(
        self,
        destination: Path,
        *,
        job_inputs: Mapping[str, Mapping[str, ShaclJobInput]] | None = None,
    ) -> ContractResult:
        """Materialize and run mandatory layers with each project's own limits.

        Composition/materialization inventory is bounded separately. Execution
        and report quotas apply per layer; there is no global backend deadline
        or cumulative contract-runner budget. Raw layer reports are retained.
        """
        return check_profile(self, destination, job_inputs=job_inputs)


def _configuration(
    local: ProjectConfig,
    ancestors: tuple[ProjectConfig, ...],
    budget: Budget,
) -> ProjectConfig:
    """Register real inherited paths while preserving local job names and aliases."""
    data = local.to_dict()
    rdf = object_value(data.get("rdf", {}), location="profile.json")
    contexts = object_value(data.get("contexts", {}), location="profile.json")
    logical = {item.document_iri for item in local.rdf.values()}
    for parent in ancestors:
        for descriptor in parent.rdf.values():
            budget.consume("work")
            iri = descriptor.document_iri
            if iri is None or iri in logical:
                continue
            bound_text(iri, budget)
            if iri in rdf:
                raise InputError(
                    "Inherited identity conflicts with a local resource alias"
                )
            path = descriptor.content
            if not isinstance(path, Path):
                raise InputError("Materialized resources require owned local paths")
            bound_text(str(path), budget)
            budget.consume("states")
            rdf[iri] = {
                "path": str(path),
                "format": descriptor.format,
                "iri": iri,
                "effective_url": descriptor.effective_url,
                "graph_name": descriptor.graph_name,
                "union": descriptor.union,
            }
            logical.add(iri)
        for iri, path in parent.contexts.items():
            budget.consume("work")
            if iri not in contexts:
                bound_text(iri, budget)
                bound_text(str(path), budget)
                budget.consume("states")
                contexts[iri] = str(path)
    data["rdf"], data["contexts"] = rdf, contexts
    return ProjectConfig(data, directory=local.directory, source=local.source)


def _selected_inventory(profile: EffectiveProfile, budget: Budget) -> None:
    """Require the declared mandatory closure before trusting an executable scope."""
    root = _text(profile.iri, budget, iri=True)
    layers: dict[str, ProfileLayer] = {}
    for raw in _items(profile.layers, budget):
        if not isinstance(raw, ProfileLayer):
            raise InputError("Profile execution requires typed layers")
        iri = _text(raw.iri, budget, iri=True)
        if type(raw.selected) is not bool or type(raw.required) is not bool:
            raise InputError("Profile layer selections require booleans")
        if iri in layers:
            raise InputError("Profile layer identifiers must be unique")
        if raw.profile is not None:
            manifest = _qualify_profile(raw.profile, budget)
            if manifest.iri != iri:
                raise InputError("Profile layers must identify their owned manifests")
        elif raw.selected:
            raise InputError("Selected profiles require owned manifests")
        if raw.selected:
            if raw.bundle is None:
                raise InputError("Selected profiles require owned bundles")
            if (
                raw.profile is None
                or not isinstance(raw.execution_digest, str)
                or len(raw.execution_digest) != 64
                or execution_digest(raw.profile, raw.bundle, budget)
                != raw.execution_digest
            ):
                raise InputError(
                    "Profile executable content differs from its composition"
                )
            actual = tuple(
                _text(object_value(job, location="profile jobs").get("name"), budget)
                for job in _items(raw.bundle.configuration.checks(), budget)
            )
            retained = tuple(_text(name, budget) for name in _items(raw.jobs, budget))
            if not actual or actual != retained:
                raise InputError("Profile layer jobs differ from their owned project")
        layers[iri] = raw
    if root not in layers or not layers[root].selected:
        raise InputError("Profile execution requires its selected root layer")
    reachable: set[str] = set()
    required = {root}
    pending = [root]
    while pending:
        budget.consume("work")
        iri = pending.pop()
        if iri in reachable:
            continue
        budget.consume("states")
        reachable.add(iri)
        layer = layers[iri]
        assert layer.profile is not None
        for parent in layer.profile.parents:
            budget.consume("work")
            target = layers.get(parent.iri)
            if target is None:
                raise InputError("Every declared parent must remain visible")
            if parent.required:
                required.add(parent.iri)
                if not target.selected:
                    raise InputError("A required parent layer cannot be omitted")
            if target.selected:
                budget.consume("states")
                pending.append(parent.iri)
    if reachable != {iri for iri, layer in layers.items() if layer.selected}:
        raise InputError("Selected layers must belong to the declared root closure")
    if any(layer.required != (iri in required) for iri, layer in layers.items()):
        raise InputError("Profile required flags differ from the declared closure")


def _materialize(
    profile: EffectiveProfile, destination: Path
) -> dict[str, ProjectConfig]:
    """Prequalify finite output inventory before creating the new owned directory."""
    budget = Budget(profile.limits)
    _selected_inventory(profile, budget)
    bound_text(str(destination), budget)
    if destination.exists():
        raise InputError("Profile destination must be a new directory")
    keys: dict[str, str] = {}
    for layer in profile.layers:
        budget.consume("states")
        if not layer.selected:
            continue
        if layer.bundle is None or layer.profile is None or not layer.jobs:
            raise InputError("Every selected profile requires an owned bundle and jobs")
        keys[layer.iri] = layer_key(layer.iri, budget)
        for name, payload in layer.bundle.files.items():
            budget.consume("resources")
            bound_text(name, budget)
            digest(payload, budget)
        digest(layer.profile.manifest, budget)
        retained = layer.bundle.files.get("profile.json")
        if retained is not None and retained != layer.profile.manifest:
            raise InputError("Profile manifest changed since acquisition")
    destination.mkdir(parents=True, exist_ok=False)
    configurations: dict[str, ProjectConfig] = {}
    ancestry: dict[str, tuple[str, ...]] = {}
    for layer in profile.layers:
        if not layer.selected:
            continue
        assert layer.bundle is not None and layer.profile is not None
        local = layer.bundle.materialize(destination / keys[layer.iri], local_only=True)
        if "profile.json" not in layer.bundle.files:
            with (local.directory / "profile.json").open("xb") as handle:
                handle.write(layer.profile.manifest)
        ancestors: list[str] = []
        for parent in layer.profile.parents:
            budget.consume("work")
            if parent.iri not in configurations:
                continue
            for ancestor in (*ancestry[parent.iri], parent.iri):
                budget.consume("work")
                if ancestor not in ancestors:
                    budget.consume("states")
                    ancestors.append(ancestor)
        ancestry[layer.iri] = tuple(ancestors)
        configurations[layer.iri] = _configuration(
            local, tuple(configurations[iri] for iri in ancestors), budget
        )
    return configurations


def check_profile(
    profile: EffectiveProfile,
    destination: Path,
    *,
    job_inputs: Mapping[str, Mapping[str, ShaclJobInput]] | None = None,
) -> ContractResult:
    """Run every selected layer with its original limits and untouched raw truth.

    Inventory and materialization use the composition limits. The existing
    contract runner applies no global backend deadline or shared report quota;
    each ordinary project independently enforces its own configured limits.
    """
    if not isinstance(destination, Path):
        raise InputError("Profile materialization requires an explicit Path")
    policy = AcceptancePolicy(strict=True, minimum_severity="info")
    if not profile.report.verified:
        return check_contract(
            (ContractObligation("composition", lambda: profile.report),), policy=policy
        )
    runtime: dict[str, dict[str, ShaclJobInput]] = {}
    try:
        inventory = Budget(profile.limits)
        if job_inputs is not None:
            if not isinstance(job_inputs, Mapping):
                raise InputError("Profile runtime inputs require named layers")
            inventory.consume("states", len(job_inputs))
            for iri, inputs in job_inputs.items():
                _text(iri, inventory, iri=True)
                runtime[iri] = capture_job_inputs(inputs, inventory)
            selected = {
                _text(layer.iri, inventory, iri=True)
                for layer in profile.layers
                if layer.selected
            }
            if runtime.keys() - selected:
                raise InputError("Runtime inputs must identify selected profile layers")
        configurations = _materialize(profile, destination)
    except (InputError, OSError, UnicodeError) as error:
        issues = IssueCollector(Budget(profile.limits))
        if isinstance(error, LimitExceeded):
            issues.exhausted(str(error))
        else:
            issues.append(
                Issue("profile.composition_unverified", str(error), kind="unverified")
            )
        failed = Report("profile-materialization", issues=tuple(issues), requested=1)
        return check_contract(
            (ContractObligation("materialization", lambda: failed),), policy=policy
        )
    budget = Budget(profile.limits)
    obligations = [ContractObligation("composition", lambda: profile.report)]
    for layer in profile.layers:
        name = layer_key(layer.iri, budget)
        if layer.selected:
            config = configurations[layer.iri]

            def run(
                configuration: ProjectConfig = config,
                inputs: Mapping[str, ShaclJobInput] | None = runtime.get(layer.iri),
            ) -> Report:
                """Retain the ordinary layer report without policy rewriting."""
                return check_project(configuration, job_inputs=inputs)

            obligations.append(ContractObligation(name, run))
        else:
            obligations.append(ContractObligation(name, None, required=False))
    return check_contract(tuple(obligations), policy=policy)
