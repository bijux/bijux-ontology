"""Exact offline resource identities independent of physical package locations."""

import hashlib
from collections.abc import Iterator

from bijux_ontology._json import object_value
from bijux_ontology.documents.archive import resource_path
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.roles.classification import bound_text
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
