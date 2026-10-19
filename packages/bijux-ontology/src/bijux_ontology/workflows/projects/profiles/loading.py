"""Strict portable profile metadata without eager child resource acquisition."""

from collections.abc import Sequence
from importlib.resources.abc import Traversable
from pathlib import Path

from rdflib import BNode
from rdflib import Literal as RdfLiteral

from bijux_ontology._json import decode_json, known_keys, object_value
from bijux_ontology.documents.archive import bundle_reader, resource_path
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import ProfileArtifact, ProfileParent, ProfileShapes, ValidationProfile


def _text(value: object, budget: Budget, *, iri: bool = False) -> str:
    """Bound JSON metadata before parsing it or retaining identifier copies."""
    if not isinstance(value, str):
        raise InputError("Profile metadata requires text")
    bound_text(value, budget)
    if not value:
        raise InputError("Profile metadata cannot be empty")
    if iri:
        if isinstance(value, BNode | RdfLiteral):
            raise InputError("Profile identifiers require named IRI text")
        try:
            absolute_iri(value)
        except InputError as error:
            raise InputError("Profile identifiers must be absolute IRIs") from error
    return str(value)


def _items(value: object, budget: Budget) -> tuple[object, ...]:
    """Own one finite inventory using one cached count and indexed reads."""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise InputError("Profile inventories require finite arrays")
    count = len(value)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = []
    for index in range(count):
        budget.consume("work")
        try:
            result.append(value[index])
        except IndexError as error:
            raise InputError("Profile inventory changed during capture") from error
    return tuple(result)


def load_profile(
    source: Path | Traversable, *, limits: Limits | None = None
) -> ValidationProfile:
    """Read profile.json only; compose_profile resolves and acquires parents first.

    The sibling project.json remains an ordinary project/bundle. Manifest loading
    does not execute checks, fetch parent IRIs or create output directories.
    """
    budget = Budget(limits)
    with bundle_reader(source, limits=budget.limits) as read:
        payload = read("profile.json")
    return _decode_profile(payload, source, budget)


def _decode_profile(
    payload: bytes, source: Path | Traversable, budget: Budget
) -> ValidationProfile:
    """Decode an owned manifest with the same finite schema as local acquisition."""
    budget.consume("bytes", len(payload))
    data = object_value(
        decode_json(
            payload,
            max_bytes=budget.limits.max_bytes,
            max_depth=budget.limits.max_depth,
        ),
        location="profile.json",
    )
    known_keys(
        data,
        {"version", "id", "parents", "shapes", "artifacts", "conflicts"},
        location="profile.json",
    )
    if type(data.get("version")) is not int or data["version"] != 1:
        raise InputError("Profile manifest version must be 1")
    iri = _text(data.get("id"), budget, iri=True)
    parents = []
    parent_ids: set[str] = set()
    for raw in _items(data.get("parents", []), budget):
        item = object_value(raw, location="profile.json")
        known_keys(item, {"id", "required"}, location="profile.json")
        name = _text(item.get("id"), budget, iri=True)
        required = item.get("required", True)
        if type(required) is not bool or name in parent_ids:
            raise InputError("Parent identifiers must be unique with boolean required")
        parent_ids.add(name)
        parents.append(ProfileParent(name, required))
    shapes = []
    sources: set[str] = set()
    for raw in _items(data.get("shapes", []), budget):
        item = object_value(raw, location="profile.json")
        known_keys(item, {"source", "roots"}, location="profile.json")
        name = _text(item.get("source"), budget)
        roots = tuple(
            _text(value, budget, iri=True)
            for value in _items(item.get("roots"), budget)
        )
        if not roots or name in sources or len(set(roots)) != len(roots):
            raise InputError(
                "Shape inventories need unique sources and nonempty unique roots"
            )
        sources.add(name)
        shapes.append(ProfileShapes(name, roots))
    artifacts = []
    artifact_ids: set[str] = set()
    for raw in _items(data.get("artifacts", []), budget):
        item = object_value(raw, location="profile.json")
        known_keys(item, {"iri", "path", "roles", "format"}, location="profile.json")
        name = _text(item.get("iri"), budget, iri=True)
        path = resource_path(_text(item.get("path"), budget))
        roles = tuple(
            _text(value, budget, iri=True)
            for value in _items(item.get("roles", []), budget)
        )
        if name in artifact_ids or len(set(roles)) != len(roles):
            raise InputError("Artifact identifiers and roles must be unique")
        artifact_ids.add(name)
        syntax = item.get("format")
        artifacts.append(
            ProfileArtifact(
                name, path, roles, None if syntax is None else _text(syntax, budget)
            )
        )
    conflict = object_value(data.get("conflicts", {}), location="profile.json")
    known_keys(conflict, {"same_shape", "severity_changes"}, location="profile.json")
    same = conflict.get("same_shape", "reject")
    severity = conflict.get("severity_changes", "reject")
    if same not in ("reject", "separate") or severity not in (
        "reject",
        "preserve_each",
    ):
        raise InputError("Unknown explicit profile conflict policy")
    return ValidationProfile(
        iri,
        tuple(parents),
        tuple(shapes),
        tuple(artifacts),
        same,
        severity,
        source,
        payload,
    )


def _qualify_profile(profile: ValidationProfile, budget: Budget) -> ValidationProfile:
    """Bound typed metadata before comparing it with its authoritative manifest."""
    if not isinstance(profile, ValidationProfile):
        raise InputError("Expected an owned profile manifest")
    _text(profile.iri, budget, iri=True)
    for parent in _items(profile.parents, budget):
        if not isinstance(parent, ProfileParent) or type(parent.required) is not bool:
            raise InputError("Profile parents require typed boolean selection records")
        _text(parent.iri, budget, iri=True)
    for inventory in _items(profile.shapes, budget):
        if not isinstance(inventory, ProfileShapes):
            raise InputError("Profile shapes require typed root inventories")
        _text(inventory.source, budget)
        for root in _items(inventory.roots, budget):
            _text(root, budget, iri=True)
    for artifact in _items(profile.artifacts, budget):
        if not isinstance(artifact, ProfileArtifact):
            raise InputError("Profile artifacts require typed resource descriptions")
        _text(artifact.iri, budget, iri=True)
        resource_path(_text(artifact.path, budget))
        for role in _items(artifact.roles, budget):
            _text(role, budget, iri=True)
        if artifact.format is not None:
            _text(artifact.format, budget)
    _text(profile.same_shape, budget)
    _text(profile.severity_changes, budget)
    if not isinstance(profile.manifest, bytes):
        raise InputError("Profile manifests require owned bytes")
    if len(profile.manifest) > budget.limits.max_bytes:
        raise LimitExceeded("Profile manifest exceeds max_bytes")
    decoded = _decode_profile(profile.manifest, profile.source, budget)
    if profile != decoded:
        raise InputError("Profile metadata conflicts with its owned manifest")
    return decoded
