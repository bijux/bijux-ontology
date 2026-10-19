"""Create explicit owned profile instances through existing bundle and project APIs."""

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.archive import resource_path
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.profiles.composition import compose_profile
from bijux_ontology.workflows.projects.profiles.identity import identities
from bijux_ontology.workflows.projects.profiles.loading import _text, load_profile
from bijux_ontology.workflows.projects.profiles.types import ValidationProfile

from .preparation import (
    InstantiationUnavailable,
    PreparedInstantiation,
    prepare_instantiation,
)
from .types import ParameterizedProfile, ParameterValue, ProfileInstance


def _key(value: str, budget: Budget) -> str:
    """Create compact stable local names after charging original metadata."""
    bound_text(value, budget)
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value: object, budget: Budget) -> bytes:
    """Bound generated ordinary configuration text before retaining its bytes."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    bound_text(text, budget)
    return text.encode()


def _write(path: Path, payload: bytes) -> None:
    """Write only inside this invocation's newly owned destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def materialize_instantiation(
    template: ParameterizedProfile,
    plan: PreparedInstantiation,
    destination: Path,
    instance_iri: str,
    budget: Operation,
) -> tuple[ValidationProfile, dict[str, ValidationProfile]]:
    """Preserve required layers and register selected resources at real local paths."""
    config = plan.configuration
    rdf = object_value(config.get("rdf", {}), location="rdf")
    contexts = object_value(config.get("contexts", {}), location="contexts")
    root = next(
        layer for layer in plan.baseline.layers if layer.iri == template.profile.iri
    )
    assert root.bundle is not None
    payloads: dict[str, bytes] = dict(plan.inventories)
    redirects: dict[str, str] = {}
    changed_formats: set[str] = set()

    def redirect(original: str, generated: str) -> None:
        """Require unique explicit member correspondence for descriptive artifacts."""
        if original in redirects and redirects[original] != generated:
            raise InstantiationUnavailable(
                "Original artifact member has ambiguous generated replacements"
            )
        redirects[original] = generated

    for alias, (syntax, payload) in plan.shapes.items():
        original = object_value(rdf[alias], location="rdf")
        path = "instantiated/shapes/" + _key(alias, budget) + ".rdf"
        if path in root.bundle.files:
            raise InputError("Generated shape member collides with an original input")
        payloads[path] = payload
        redirect(resource_path(original["path"]), path)
        rdf[alias] = original | {"path": path, "format": syntax}
    resource_directories: dict[str, tuple[Path, object]] = {}
    resource_metadata = replace(template.profile, artifacts=())
    retained_identities = dict(identities(resource_metadata, root.bundle, budget))
    for alias, selection in plan.resources.items():
        budget.consume("work")
        prefix = "instantiated/resources/" + _key(alias, budget)
        for name in root.bundle.files:
            budget.consume("work", min(len(name), len(prefix)) + 1)
            if name == prefix or name.startswith(prefix + "/"):
                raise InputError(
                    "Generated resource directory collides with an original input"
                )
        selected_identities = dict(
            identities(resource_metadata, selection.bundle, budget)
        )
        selected_config = selection.bundle.configuration.to_dict()
        selected_rdf = object_value(selected_config.get("rdf", {}), location="rdf")
        selected_descriptor = selection.bundle.rdf[selection.source]
        original_entry = object_value(rdf[alias], location="rdf")
        old_path = resource_path(original_entry["path"])
        selected_entry = object_value(selected_rdf[selection.source], location="rdf")
        redirect(old_path, prefix + "/" + resource_path(selected_entry["path"]))
        if root.bundle.rdf[alias].format != selected_descriptor.format:
            changed_formats.add(old_path)
        budget.consume("work", len(rdf))
        budget.consume("states", len(rdf))
        logical = {
            object_value(entry, location="rdf").get("iri"): name
            for name, entry in rdf.items()
            if name != alias
        }
        for name, descriptor in selection.bundle.rdf.items():
            budget.consume("work")
            entry = object_value(selected_rdf[name], location="rdf")
            if descriptor.document_iri is None:
                raise InputError(
                    "Selected resources require explicit logical identities"
                )
            target_alias = (
                alias if name == selection.source else descriptor.document_iri
            )
            if name != selection.source and descriptor.document_iri in logical:
                key = ("rdf", descriptor.document_iri)
                if selected_identities[key] != retained_identities.get(key):
                    raise InputError(
                        "Selected import closure conflicts with "
                        "an existing RDF identity"
                    )
                continue
            if target_alias in rdf and target_alias != alias:
                raise InputError(
                    "Selected resource identity conflicts with a local alias"
                )
            rdf[target_alias] = entry | {
                "path": prefix + "/" + resource_path(entry["path"]),
                "format": descriptor.format,
                "iri": descriptor.document_iri,
                "effective_url": descriptor.effective_url,
                "graph_name": descriptor.graph_name,
                "union": descriptor.union,
            }
        for iri, context_path in object_value(
            selected_config.get("contexts", {}), location="contexts"
        ).items():
            budget.consume("work")
            if iri in contexts:
                key = ("context", iri)
                if selected_identities[key] != retained_identities.get(key):
                    raise InputError(
                        "Selected resource context conflicts with an existing binding"
                    )
                continue
            contexts[iri] = prefix + "/" + resource_path(context_path)
        if selected_descriptor.document_iri != object_value(
            rdf[alias], location="rdf"
        ).get("iri"):
            raise InputError("Selected resource logical identity was not retained")
        retained_identities.update(selected_identities)
        resource_directories[alias] = destination / "profile" / prefix, selection.bundle
    config["rdf"], config["contexts"] = rdf, contexts
    # Public validation happens before any output, with exactly the paths written below.
    configuration = ProjectConfig(
        config, directory=destination / "profile", source="project.json"
    )
    inputs = configuration.input_paths()
    budget.consume("states", len(inputs))
    budget.consume("bytes", 64 + 16 * len(inputs))
    live_members = set()
    for input_path in inputs:
        bound_text(str(input_path), budget)
        try:
            member = input_path.relative_to(configuration.directory).as_posix()
        except ValueError as error:
            raise InstantiationUnavailable(
                "Instance inputs require owned local members"
            ) from error
        bound_text(member, budget)
        live_members.add(member)
    encoded_config = _json(config, budget)
    manifest = object_value(
        decode_json(
            template.profile.manifest,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        ),
        location="profile.json",
    )
    manifest["id"] = instance_iri
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise InputError("Profile artifacts require an ordinary manifest array")
    for raw in artifacts:
        budget.consume("work")
        artifact = object_value(raw, location="artifacts")
        original_path = resource_path(artifact["path"])
        if original_path in changed_formats and artifact.get("format") is not None:
            raise InstantiationUnavailable(
                "Resource syntax change cannot retain a declared artifact format"
            )
        if original_path in redirects:
            if original_path in live_members:
                raise InstantiationUnavailable(
                    "Artifact member remains in use alongside its generated replacement"
                )
            artifact["path"] = redirects[original_path]
    encoded_manifest = _json(manifest, budget)
    for layer in plan.baseline.layers:
        if not layer.selected:
            continue
        assert layer.bundle is not None and layer.profile is not None
        for name, payload in layer.bundle.files.items():
            budget.consume("resources")
            bound_text(name, budget)
            if len(payload) > budget.limits.max_bytes:
                raise LimitExceeded("Profile instance member exceeds max_bytes")
            budget.consume("bytes", len(payload))
        budget.consume("bytes", len(layer.profile.manifest))
    for selection in plan.resources.values():
        for name, payload in selection.bundle.files.items():
            budget.consume("resources")
            bound_text(name, budget)
            if len(payload) > budget.limits.max_bytes:
                raise LimitExceeded("Selected resource member exceeds max_bytes")
            budget.consume("bytes", len(payload))
    destination.mkdir(parents=True, exist_ok=False)
    registry = {}
    for layer in plan.baseline.layers:
        if not layer.selected:
            continue
        assert layer.bundle is not None and layer.profile is not None
        directory = destination / (
            "profile"
            if layer.iri == template.profile.iri
            else "parents/" + _key(layer.iri, budget)
        )
        layer.bundle.materialize(directory, local_only=True)
        _write(directory / "profile.json", layer.profile.manifest)
        if layer.iri != template.profile.iri:
            registry[layer.iri] = load_profile(directory, limits=budget.limits)
    for directory, bundle in resource_directories.values():
        from bijux_ontology.workflows.projects.bundles import PreparedBundle

        assert isinstance(bundle, PreparedBundle)
        bundle.materialize(directory)
    for path, payload in payloads.items():
        budget.consume("work")
        _write(destination / "profile" / resource_path(path), payload)
    _write(destination / "profile/project.json", encoded_config)
    _write(destination / "profile/profile.json", encoded_manifest)
    return load_profile(destination / "profile", limits=budget.limits), registry


def instantiate_profile(
    template: ParameterizedProfile,
    bindings: Mapping[str, ParameterValue],
    *,
    instance_iri: str,
    destination: Path,
    parents: Mapping[str, ValidationProfile],
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> ProfileInstance:
    """Compile a finite typed instance; preparation never executes its data checks.

    Only ordinary complete graph shape resources and named authored shape IDs
    are supported. Resource bundles must be locally closed. Unsupported dataset
    replacement/anonymous association stays unknown. Outputs use a new explicit
    directory; a failed write/composition can leave bounded review artifacts,
    never an executable verified ProfileInstance. Actual project execution uses
    the existing effective profile's independent per-layer limits.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    profile = None
    effective = None
    checked = 0
    try:
        name = _text(instance_iri, operation, iri=True)
        if not isinstance(destination, Path):
            raise InputError(
                "Profile instantiation requires an explicit destination Path"
            )
        bound_text(str(destination), operation)
        if destination.exists():
            raise InputError("Profile instance destination must be a new directory")
        plan = prepare_instantiation(template, bindings, parents, operation, issues)
        if name in {
            layer.iri
            for layer in plan.baseline.layers
            if layer.iri != template.profile.iri
        }:
            raise InputError("An instance cannot replace a declared parent identity")
        profile, registry = materialize_instantiation(
            template, plan, destination, name, operation
        )
        effective = compose_profile(
            profile,
            registry,
            budget=operation,
            timeout=max(0.001, operation.deadline - time.monotonic()),
            cancel=cancel,
        )
        retain_report(effective.report, operation, issues)
        if not effective.report.verified:
            raise InstantiationUnavailable(
                "Bound ordinary profile composition is unverified"
            )
        checked = 1
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError, UnicodeError) as error:
        try:
            bound_text(str(error), operation)
            issues.append(
                Issue(
                    "profile.instantiation_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, InstantiationUnavailable)
                    else "input_error",
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
        profile, effective = None, None
    report = Report(
        "profile-instantiation",
        checked,
        tuple(issues),
        requested=1,
        regime="typed-profile-instantiation",
    )
    if not report.verified:
        profile, effective = None, None
    return ProfileInstance(profile, effective, report)
