"""Exact offline resource identities independent of physical package locations."""

import hashlib
from collections.abc import Iterator

from bijux_ontology._json import object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.documents.archive import resource_path
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.workflows.projects.bundles import PreparedBundle

from .types import ValidationProfile


def digest(payload: bytes, budget: Budget) -> str:
    """Reserve supplied bytes and hashing work before producing a fingerprint."""
    if len(payload) > budget.limits.max_bytes:
        raise LimitExceeded("Profile member exceeds max_bytes")
    budget.consume("bytes", len(payload))
    budget.consume("work", len(payload) + 1)
    return hashlib.sha256(payload).hexdigest()


def layer_key(iri: str, budget: Budget) -> str:
    """Use a fixed-size stable output/report key while retaining original IDs."""
    bound_text(iri, budget)
    return "profile-" + hashlib.sha256(iri.encode()).hexdigest()


def identities(
    profile: ValidationProfile, bundle: PreparedBundle, budget: Budget
) -> Iterator[tuple[tuple[str, str], tuple[object, ...]]]:
    """Qualify every local owned file and each declared logical binding once."""
    count = len(bundle.files)
    budget.consume("states", count)
    hashes: dict[str, str] = {}
    for path, payload in bundle.files.items():
        budget.consume("resources")
        bound_text(path, budget)
        hashes[path] = digest(payload, budget)
    config = bundle.configuration.to_dict()
    rdf = object_value(config.get("rdf", {}), location="profile.json")
    for name, descriptor in bundle.rdf.items():
        budget.consume("states")
        item = object_value(rdf[name], location="profile.json")
        path = resource_path(item["path"])
        metadata = (
            descriptor.format,
            descriptor.document_iri,
            descriptor.effective_url,
            descriptor.graph_name,
            descriptor.union,
            hashes[path],
        )
        for value in metadata[:-1]:
            if isinstance(value, str):
                bound_text(value, budget)
        for iri in (descriptor.document_iri, descriptor.effective_url):
            if iri is not None:
                yield ("rdf", iri), metadata
    for iri, raw in object_value(
        config.get("contexts", {}), location="profile.json"
    ).items():
        budget.consume("states")
        bound_text(iri, budget)
        yield ("context", iri), (hashes[resource_path(raw)],)
    for artifact in profile.artifacts:
        budget.consume("states")
        bound_text(artifact.iri, budget)
        if artifact.path not in hashes:
            raise InputError("Described profile artifact is not an owned project input")
        yield ("artifact", artifact.iri), (artifact.format, hashes[artifact.path])


def execution_digest(
    profile: ValidationProfile, bundle: PreparedBundle, budget: Budget
) -> str:
    """Fingerprint the exact manifest, configuration and files executed locally.

    Materialization reconstructs projects solely from these owned values; neither
    physical source paths nor caller object identities establish correspondence.
    """
    fingerprint = hashlib.sha256()

    def text(value: str) -> None:
        """Hash one unambiguous bounded text frame."""
        bound_text(value, budget)
        payload = value.encode("utf-8")
        fingerprint.update(str(len(payload)).encode("ascii") + b":" + payload)

    text("manifest")
    text(digest(profile.manifest, budget))
    count = len(bundle.files)
    budget.consume("states", count)
    text("files")
    text(str(count))
    names = []
    for name, payload in bundle.files.items():
        budget.consume("work")
        bound_text(name, budget)
        if not isinstance(payload, bytes):
            raise InputError("Executable profile files require owned bytes")
        names.append(name)
    budget.consume("work", count * max(1, count.bit_length()))
    for name in sorted(names):
        text(name)
        text(digest(bundle.files[name], budget))

    def value(item: object, depth: int = 0) -> None:
        """Frame the finite JSON configuration without formatting unbounded text."""
        budget.consume("work")
        budget.consume("states")
        if depth > budget.limits.max_depth:
            raise LimitExceeded("Profile configuration exceeds max_depth")
        if isinstance(item, dict):
            text("object")
            text(str(len(item)))
            budget.consume("states", len(item))
            for key in item:
                bound_text(key, budget)
            budget.consume("work", len(item) * max(1, len(item).bit_length()))
            for key in sorted(item):
                text(key)
                value(item[key], depth + 1)
        elif isinstance(item, list):
            text("array")
            text(str(len(item)))
            for child in item:
                value(child, depth + 1)
        elif isinstance(item, str):
            text("string")
            text(item)
        elif item is None:
            text("null")
        elif type(item) is bool:
            text("true" if item else "false")
        elif type(item) is int:
            budget.consume("work", item.bit_length() + 1)
            text("integer")
            text(str(item))
        elif type(item) is float:
            text("float")
            text(repr(item))
        else:
            raise InputError("Executable configuration requires finite JSON values")

    text("configuration")
    value(bundle.configuration.to_dict())
    return fingerprint.hexdigest()
