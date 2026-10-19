"""Parent-first offline profile composition with retained mandatory layer jobs."""

import threading
from collections.abc import Mapping, Sequence

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.reports import Issue, Report
from bijux_ontology.workflows.projects.bundles import PreparedBundle, load_bundle

from .conflicts import shape_conflicts
from .execution import EffectiveProfile
from .identity import execution_digest, identities
from .loading import _items, _qualify_profile, _text
from .types import ProfileDecision, ProfileLayer, ValidationProfile


def compose_profile(
    root: ValidationProfile,
    profiles: Mapping[str, ValidationProfile],
    *,
    optional_layers: Sequence[str] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30.0,
    cancel: threading.Event | None = None,
) -> EffectiveProfile:
    """Resolve only supplied manifests, then load selected bundles parent-first.

    Excluded optional layers remain visible; all checks within each selected
    ordinary project remain mandatory. No child field replaces a parent check.
    Resource IDs, bases, formats, graph selections and bytes must agree exactly.
    """
    if not isinstance(root, ValidationProfile) or not isinstance(profiles, Mapping):
        raise InputError(
            "Profile composition requires loaded manifests and a finite registry"
        )
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    layers: list[ProfileLayer] = []
    decisions: tuple[ProfileDecision, ...] = ()
    qualified_iri = ""
    try:
        root = _qualify_profile(root, operation)
        qualified_iri = root.iri
        count = len(profiles)
        operation.consume("states", count)
        registry = {root.iri: root}
        for iri, registered_profile in profiles.items():
            operation.consume("work")
            iri = _text(iri, operation, iri=True)
            registered_profile = _qualify_profile(registered_profile, operation)
            if iri != registered_profile.iri:
                raise InputError(
                    "Registry keys must identify their exact loaded profiles"
                )
            if iri == root.iri and registered_profile != root:
                raise InputError("Root profile cannot be shadowed in its registry")
            registry[iri] = registered_profile
        optional = {
            _text(value, operation, iri=True)
            for value in _items(optional_layers, operation)
        }
        ordered: list[str] = []
        active: set[str] = set()
        seen: set[str] = set()
        declared_optional: set[str] = set()

        def visit(iri: str, depth: int) -> None:
            """Capture declared dependency edges, including visible exclusions."""
            operation.consume("work")
            operation.consume("states")
            if depth > min(operation.limits.max_depth, 128):
                raise LimitExceeded("Profile dependencies exceed max_depth")
            if iri in active:
                raise InputError("Cyclic profile dependency")
            if iri in seen:
                return
            active.add(iri)
            profile = registry.get(iri)
            if profile is not None:
                _text(profile.iri, operation, iri=True)
                if len(profile.manifest) > operation.limits.max_bytes:
                    raise LimitExceeded("Profile manifest exceeds max_bytes")
                operation.consume("bytes", len(profile.manifest))
                for parent in _items(profile.parents, operation):
                    from .types import ProfileParent

                    if (
                        not isinstance(parent, ProfileParent)
                        or type(parent.required) is not bool
                    ):
                        raise InputError(
                            "Profile parents require typed selection records"
                        )
                    parent_id = _text(parent.iri, operation, iri=True)
                    if not parent.required:
                        declared_optional.add(parent_id)
                    visit(parent_id, depth + 1)
            active.remove(iri)
            seen.add(iri)
            ordered.append(iri)

        visit(root.iri, 0)
        if optional - declared_optional:
            raise InputError("Only explicitly declared optional layers can be selected")
        selected = {root.iri}
        required = {root.iri}
        for iri in reversed(ordered):
            operation.consume("work")
            if iri not in selected:
                continue
            profile = registry.get(iri)
            if profile is None:
                raise InputError("A selected profile dependency is unavailable")
            for parent in profile.parents:
                operation.consume("work")
                if parent.required or parent.iri in optional:
                    selected.add(parent.iri)
                    if parent.required:
                        required.add(parent.iri)
        bundles: dict[str, PreparedBundle] = {}
        registered: dict[tuple[str, str], tuple[object, ...]] = {}
        for iri in ordered:
            operation.consume("work")
            profile = registry.get(iri)
            if iri not in selected:
                layers.append(ProfileLayer(iri, profile, None, False, False))
                continue
            assert profile is not None
            dependencies = tuple(
                bundles[parent.iri]
                for parent in profile.parents
                if parent.iri in selected
            )
            bundle = load_bundle(
                profile.source, limits=operation.limits, dependencies=dependencies
            )
            for key, identity in identities(profile, bundle, operation):
                if key in registered and registered[key] != identity:
                    raise InputError("Conflicting profile resource identity")
                registered[key] = identity
            jobs = bundle.configuration.checks()
            names = tuple(_text(job.get("name"), operation) for job in jobs)
            bundles[iri] = bundle
            layers.append(
                ProfileLayer(
                    iri,
                    profile,
                    bundle,
                    iri in required,
                    True,
                    names,
                    execution_digest=execution_digest(profile, bundle, operation),
                )
            )
        decisions = shape_conflicts(tuple(layers), operation, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError, UnicodeError) as error:
        try:
            issues.append(
                Issue("profile.composition_unverified", str(error), kind="unverified")
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "profile-composition",
        len(layers),
        tuple(issues),
        requested=max(1, len(layers)),
        regime="offline-mandatory-profile-composition",
    )
    return EffectiveProfile(
        qualified_iri, tuple(layers), decisions, report, operation.limits
    )
