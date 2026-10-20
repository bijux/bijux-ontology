"""Explicit local classpaths, immutable file seals and native preconditions."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.json.values import decode_json, object_value
from bijux_ontology.rdf.stores.seals import seal_files, verify_files

from ..types import OwlProfileBackend

OWLAPI_VERSION = "5.5.1"
MAIN_CLASS = "org.bijux.ontology.profiles.ProfileWorker"
LOADER_PROFILE = "turtle-permissive-no-punning-repair-offline"
PROFILES = ("owl2-dl", "owl2-el", "owl2-ql", "owl2-rl")


def prepare_profile_backend(
    java: Path,
    bundle: Path,
    *,
    manifest_sha256: str,
    artifacts: Path | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> OwlProfileBackend:
    """Qualify an explicit local bundle; never discover Java or download binaries.

    The flat manifest names the exact ordered classpath and SHA256 of every jar.
    Initial content seals and per-operation revision checks reuse core primitives.
    Java availability/version is observed only by explicit capability/check calls.
    """
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Backend limits conflict with the shared operation budget")
    operation = budget if budget is not None else Budget(limits)
    if not isinstance(java, Path) or not isinstance(bundle, Path):
        raise InputError("OWL backend requires explicit Java and bundle Paths")
    bound_text(str(java), operation)
    bound_text(str(bundle), operation)
    bound_text(manifest_sha256, operation)
    if len(manifest_sha256) != 64 or any(
        c not in "0123456789abcdef" for c in manifest_sha256
    ):
        raise InputError("OWL bundle manifest requires a lowercase SHA256")
    if not java.is_absolute():
        raise InputError("Java executable must be an explicit absolute Path")
    directory = bundle.resolve()
    if os.pathsep in str(directory):
        raise ResourceError("OWL bundle path contains a Java classpath separator")
    if bundle.is_symlink() or not directory.is_dir():
        raise ResourceError("OWL bundle must be an ordinary supplied directory")
    path = directory / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ResourceError("OWL bundle manifest is unavailable")
    with path.open("rb") as stream:
        raw = stream.read(operation.limits.max_bytes + 1)
    operation.consume("bytes", len(raw))
    if len(raw) > operation.limits.max_bytes:
        raise LimitExceeded("OWL bundle manifest exceeds max_bytes")
    operation.consume("work", len(raw))
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ResourceError("OWL bundle manifest differs from its explicit pin")
    manifest = object_value(
        decode_json(
            raw,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        ),
        location="OWL bundle manifest",
    )
    operation.consume("bytes", json_bound(manifest, operation))
    if set(manifest) != {
        "protocol",
        "owlapi",
        "main_class",
        "loader_profile",
        "jars",
    } or (
        type(manifest["protocol"]) is not int
        or manifest["protocol"] != 1
        or manifest["owlapi"] != OWLAPI_VERSION
        or manifest["main_class"] != MAIN_CLASS
        or manifest["loader_profile"] != LOADER_PROFILE
    ):
        raise ResourceError("Unsupported OWL profile bundle identity")
    entries = manifest["jars"]
    if not isinstance(entries, list) or not entries:
        raise ResourceError("OWL bundle has no explicit ordered classpath")
    operation.consume("states", len(entries))
    operation.consume("resources", len(entries))
    expected: dict[str, str] = {}
    for entry in entries:
        item = object_value(entry, location="OWL classpath")
        if set(item) != {"name", "sha256"}:
            raise ResourceError("Invalid OWL classpath record")
        name, digest = item["name"], item["sha256"]
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ResourceError("OWL classpath identities must be text")
        bound_text(name, operation)
        bound_text(digest, operation)
        if (
            not name.endswith(".jar")
            or Path(name).name != name
            or os.pathsep in name
            or name in expected
        ):
            raise ResourceError("OWL classpath requires unique flat jar names")
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ResourceError("Invalid OWL classpath content hash")
        expected[name] = digest
    files = seal_files(directory, operation)
    operation.consume("states", len(files))
    actual = {item.name: item.sha256 for item in files}
    if actual.get("manifest.json") != manifest_sha256 or any(
        actual.get(name) != digest for name, digest in expected.items()
    ):
        raise ResourceError("OWL classpath content differs from the pinned manifest")
    destination = (
        artifacts
        if artifacts is not None
        else Path.cwd() / "artifacts" / "owl-profiles"
    )
    if not isinstance(destination, Path):
        raise InputError("OWL operation artifacts require a Path")
    bound_text(str(destination), operation)
    return OwlProfileBackend._owned(
        java=java,
        directory=directory,
        manifest_sha256=manifest_sha256,
        jars=tuple(expected),
        files=files,
        artifacts=destination.absolute(),
    )


def qualify_backend(backend: OwlProfileBackend, budget: Budget) -> None:
    """Reject changed bundles and ambient JVM injection before native execution."""
    if not isinstance(backend, OwlProfileBackend):
        raise ResourceError("An explicitly prepared OWL profile backend is required")
    if os.name != "posix":
        raise ResourceError(
            "This OWL backend profile requires qualified POSIX ownership"
        )
    if any(
        name in os.environ
        for name in (
            "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS",
            "_JAVA_OPTIONS",
        )
    ):
        raise ResourceError(
            "Ambient JVM option variables are outside the supported profile"
        )
    bound_text(str(backend.java), budget)
    bound_text(str(backend.artifacts), budget)
    bound_text(backend.manifest_sha256, budget)
    directory = str(backend.directory)
    bound_text(directory, budget)
    if os.pathsep in directory or any(os.pathsep in name for name in backend.jars):
        raise ResourceError("OWL bundle paths contain a Java classpath separator")
    verify_files(backend.directory, backend.files, budget)
    if not backend.java.is_file():
        raise ResourceError("The explicit Java executable is unavailable")
