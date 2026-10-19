"""Pinned local Java artifacts and owned bounded Jena byte exchange."""

import hashlib
import json
import os
import time
from pathlib import Path

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.processes.transport import exchange_worker
from bijux_ontology.rdf.stores.seals import seal_files, verify_files
from bijux_ontology.rdf.terms import bound_text

from .types import JenaBackend

JENA_VERSION = "6.2.0"
MAIN_CLASS = "org.bijux.ontology.jena.JenaWorker"


def prepare_jena_backend(
    java: Path,
    bundle: Path,
    *,
    manifest_sha256: str,
    artifacts: Path,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> JenaBackend:
    """Seal an explicit local classpath without installation or Java discovery."""
    operation = Operation(limits, budget, 30, None)
    for path in (java, bundle, artifacts):
        if not isinstance(path, Path):
            raise InputError("Jena requires explicit Java, bundle and artifact Paths")
        bound_text(str(path), operation)
    if not java.is_absolute() or bundle.is_symlink() or not bundle.is_dir():
        raise ResourceError("The explicit ordinary Jena runtime is unavailable")
    if os.pathsep in str(bundle.absolute()):
        raise ResourceError("Jena bundle paths cannot contain classpath separators")
    if type(manifest_sha256) is not str:
        raise InputError("Jena bundle pins require plain SHA256 text")
    bound_text(manifest_sha256, operation)
    if len(manifest_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in manifest_sha256
    ):
        raise InputError("Jena bundle pins require a lowercase SHA256")
    path = bundle / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ResourceError("Jena bundle manifest is unavailable")
    size = path.stat().st_size
    if size > operation.limits.max_bytes:
        raise LimitExceeded("Jena manifest exceeds max_bytes")
    operation.consume("bytes", 32 * size + 1024)
    operation.consume("work", size + 1)
    with path.open("rb") as stream:
        raw = stream.read(size + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ResourceError("Jena manifest differs from its supplied content pin")
    manifest = object_value(decode_json(raw), location="Jena bundle")
    if set(manifest) != {"protocol", "jena", "main_class", "jars"} or (
        type(manifest["protocol"]) is not int
        or manifest["protocol"] != 1
        or manifest["jena"] != JENA_VERSION
        or manifest["main_class"] != MAIN_CLASS
    ):
        raise ResourceError("Unsupported Jena bundle identity")
    entries = manifest["jars"]
    if not isinstance(entries, list) or not entries:
        raise ResourceError("Jena requires an explicit ordered classpath")
    operation.consume("bytes", json_bound(manifest, operation))
    operation.consume("states", len(entries))
    operation.consume("resources", len(entries))
    expected: dict[str, str] = {}
    for entry in entries:
        item = object_value(entry, location="Jena jar")
        if set(item) != {"name", "sha256"}:
            raise ResourceError("Invalid Jena classpath entry")
        name, digest = item["name"], item["sha256"]
        if type(name) is not str or type(digest) is not str:
            raise ResourceError("Jena artifact identities require plain text")
        bound_text(name, operation)
        bound_text(digest, operation)
        if (
            not name.endswith(".jar")
            or Path(name).name != name
            or os.pathsep in name
            or name in expected
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ResourceError("Invalid Jena artifact content identity")
        expected[name] = digest
    operation.consume("bytes", 65536)
    files = seal_files(bundle, operation)
    operation.consume("bytes", 128 + len(files) * 128)
    actual = {file.name: file.sha256 for file in files}
    if (
        actual.get("manifest.json") != manifest_sha256
        or set(actual) != {"manifest.json", *expected}
        or any(actual[name] != digest for name, digest in expected.items())
    ):
        raise ResourceError("Jena artifacts differ from their pinned classpath")
    return JenaBackend._owned(
        java=java,
        directory=bundle.absolute(),
        jars=tuple(expected),
        files=files,
        artifacts=artifacts.absolute(),
        manifest_sha256=manifest_sha256,
    )


def invoke_jena(
    backend: JenaBackend, payload: bytes, operation: Operation, heap_mb: int
) -> bytes:
    """Own one opaque native process and prepay its bounded response workspace."""
    if type(backend) is not JenaBackend:
        raise InputError("Jena needs an explicitly prepared runtime")
    if type(heap_mb) is not int or not 32 <= heap_mb <= 4096:
        raise InputError("Jena heap must be an integer from 32 to 4096 MiB")
    if os.name != "posix":
        raise ResourceError("This Jena profile requires qualified POSIX ownership")
    if any(
        name in os.environ
        for name in (
            "JAVA_TOOL_OPTIONS",
            "JDK_JAVA_OPTIONS",
            "_JAVA_OPTIONS",
            "SIS_DATA",
        )
    ):
        raise ResourceError("Ambient Java/CRS configuration is outside this profile")
    for path in (backend.java, backend.directory, backend.artifacts):
        bound_text(str(path), operation)
    if not backend.java.is_file():
        raise ResourceError("The explicit Jena Java executable is unavailable")
    verify_files(backend.directory, backend.files, operation)
    cap = min(
        operation.limits.max_bytes,
        8_000_000,
        operation.remaining("bytes") // 128,
        operation.remaining("work") // 32,
    )
    if cap < 1024:
        raise LimitExceeded("No bounded Jena response allowance remains")
    operation.consume("bytes", 64 * cap)
    operation.consume("work", 9 * cap)
    native_states = min(cap // 8, operation.remaining("states") // 4)
    native_triples = min(cap // 8, operation.remaining("triples") // 4)
    if min(native_states, native_triples) < 1:
        raise LimitExceeded("No native Jena wrapper reservation remains")
    operation.consume("states", native_states)
    operation.consume("triples", native_triples)
    operation.consume("bytes", 16 * len(payload) + 1024)
    operation.consume("work", 4 * len(payload) + 1)
    request = object_value(decode_json(payload), location="owned Jena request")
    request.update(
        control_work=cap,
        control_states=native_states,
        control_triples=native_triples,
    )
    size = json_bound(request, operation)
    operation.consume("bytes", 2 * size)
    payload = json.dumps(request).encode()
    if len(payload) > cap * 8:
        raise LimitExceeded("Jena request exceeds its native protocol bound")
    operation.consume("bytes", 256 + 64 * len(backend.jars))
    paths = []
    for name in backend.jars:
        bound_text(name, operation)
        artifact_path = str(backend.directory / name)
        bound_text(artifact_path, operation)
        if os.pathsep in artifact_path:
            raise ResourceError("Jena classpath entries cannot contain separators")
        paths.append(artifact_path)
    command = [
        str(backend.java),
        f"-Xmx{heap_mb}m",
        "-cp",
        os.pathsep.join(paths),
        MAIN_CLASS,
        str(cap),
    ]
    operation.consume("work", 0)
    raw = exchange_worker(
        command,
        payload,
        cap=cap,
        timeout=operation.deadline - time.monotonic(),
        cancel=operation.cancel,
    )
    verify_files(backend.directory, backend.files, operation)
    operation.consume("work", len(raw) + 1)
    response = object_value(decode_json(raw), location="Jena response")
    if (
        type(response.get("protocol")) is not int
        or response.get("protocol") != 1
        or response.get("jena") != JENA_VERSION
    ):
        raise ResourceError("Jena response omitted its qualified backend identity")
    java_version, feature = response.get("java"), response.get("java_feature")
    if type(java_version) is not str or type(feature) is not int or feature < 21:
        raise ResourceError("Jena response omitted its supported Java identity")
    bound_text(java_version, operation)
    controlled = object_value(
        response.get("controlled"), location="Jena wrapper receipt"
    )
    if set(controlled) != {"work", "states", "triples"}:
        raise ResourceError("Jena omitted its controlled reservation receipt")
    for kind, maximum in (
        ("work", cap),
        ("states", native_states),
        ("triples", native_triples),
    ):
        value = controlled[kind]
        if type(value) is not int or not 0 <= value <= maximum:
            raise ResourceError("Jena wrapper exceeded its prepaid reservation")
    return raw
