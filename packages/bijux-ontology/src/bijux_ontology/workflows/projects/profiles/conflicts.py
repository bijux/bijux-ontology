"""Scoped authored shape comparison without graph union or severity rewriting."""

import time

from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import IssueCollector, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation, compare_witnesses
from bijux_ontology.rdf.patches.planning import capture_source
from bijux_ontology.reports import Issue
from bijux_ontology.shacl.profiles.extraction import extract_profile
from bijux_ontology.shacl.profiles.types import ShapeProfile

from .loading import _items, _text
from .types import ProfileDecision, ProfileLayer


def shape_conflicts(
    layers: tuple[ProfileLayer, ...], budget: Operation, issues: IssueCollector
) -> tuple[ProfileDecision, ...]:
    """Require equivalent supported closure or explicit separate original jobs.

    Isomorphism establishes finite authored RDF structure, not universal SHACL
    equivalence. Unknown anonymous/dynamic dependencies never prove equality.
    Separate policy deliberately retains both ordinary validation contexts.
    """
    previous: dict[str, list[tuple[str, ShapeProfile]]] = {}
    decisions = []
    for layer in layers:
        if not layer.selected or layer.profile is None or layer.bundle is None:
            continue
        bundle, manifest = layer.bundle, layer.profile
        jobs = bundle.configuration.checks()
        expected: dict[str, set[str]] = {}
        for job in jobs:
            budget.consume("work")
            if job.get("kind") == "shacl":
                alias = _text(job.get("shapes"), budget)
                roots = {
                    _text(root, budget, iri=True)
                    for root in _items(job.get("use_shapes"), budget)
                }
                if not roots:
                    raise InputError(
                        "Profile SHACL jobs require explicit nonempty named roots"
                    )
                expected.setdefault(alias, set()).update(roots)
        declared = {
            inventory.source: set(inventory.roots) for inventory in manifest.shapes
        }
        if expected != declared:
            raise InputError(
                "Profile shapes must inventory every selected SHACL job root"
            )
        for inventory in manifest.shapes:
            budget.consume("work")
            descriptor = bundle.rdf.get(
                inventory.source, bundle.imports.get(inventory.source)
            )
            if descriptor is None:
                raise InputError(
                    "Shape inventory references an unavailable RDF resource"
                )
            source = prepare_source(
                descriptor, budget=budget, document_loader=bundle.document_loader
            )
            for root in inventory.roots:
                budget.consume("work")
                budget.consume("states")
                remaining = budget.deadline - time.monotonic()
                if remaining <= 0:
                    raise LimitExceeded(
                        "Profile shape comparison exceeded its deadline"
                    )
                current = extract_profile(
                    source,
                    (root,),
                    budget=budget,
                    timeout=remaining,
                    cancel=budget.cancel,
                )
                for before_name, before in previous.get(root, ()):
                    budget.consume("work")
                    if (
                        manifest.same_shape == "separate"
                        and manifest.severity_changes == "preserve_each"
                    ):
                        budget.consume("states")
                        decisions.append(ProfileDecision(root, before_name, layer.iri))
                        continue
                    if (
                        not before.structural_report.verified
                        or not current.structural_report.verified
                        or before.source is None
                        or current.source is None
                    ):
                        retain_report(before.structural_report, budget, issues)
                        retain_report(current.structural_report, budget, issues)
                        issues.append(
                            Issue(
                                "profile.shape_unverified",
                                "Named shape lacks qualified finite "
                                "dependency correspondence",
                                kind="unverified",
                            )
                        )
                        continue
                    equal, _ = compare_witnesses(
                        capture_source(before.source, "before:", budget).witness,
                        capture_source(current.source, "after:", budget).witness,
                        budget,
                    )
                    if not equal:
                        issues.append(
                            Issue(
                                "profile.shape_conflict",
                                "Different named shapes require explicit "
                                "separate/preserve_each policy",
                            )
                        )
                budget.consume("states")
                previous.setdefault(root, []).append((layer.iri, current))
    return tuple(decisions)
