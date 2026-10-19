"""Explicit pinned DOSDP distribution capture without installation or discovery."""

import hashlib
from pathlib import Path

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.stores.seals import seal_files

from .types import DosdpQueryBackend

DOSDP_VERSION = "0.20.0"
MAIN_CLASS = "org.monarchinitiative.dosdp.cli.Main"


def prepare_dosdp_backend(
    java: Path,
    bundle: Path,
    *,
    manifest_sha256: str,
    artifacts: Path,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> DosdpQueryBackend:
    """Seal an explicit flat jar bundle and its caller-pinned ordered manifest.

    The maintained release has its own OWLAPI dependency set. It must remain a
    separate classpath from other native reasoning backends. Sealing reads all
    jar bytes, so callers must supply sufficient cumulative preparation limits.
    """
    if (
        limits is not None
        and not isinstance(limits, Limits)
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("DOSDP capture requires Limits and Budget objects")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("DOSDP capture limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    if any(not isinstance(path, Path) for path in (java, bundle, artifacts)):
        raise InputError("DOSDP requires explicit Java, bundle and artifact Paths")
    for path in (java, bundle, artifacts):
        bound_text(str(path), operation)
    if not java.is_absolute():
        raise InputError("DOSDP Java executable must be an absolute Path")
    bound_text(manifest_sha256, operation)
    if (
        type(manifest_sha256) is not str
        or len(manifest_sha256) != 64
        or any(char not in "0123456789abcdef" for char in manifest_sha256)
    ):
        raise InputError("DOSDP manifest requires a lowercase SHA256 pin")
    if bundle.is_symlink() or not bundle.is_dir():
        raise ResourceError("DOSDP bundle directory is unavailable")
    directory = bundle.resolve()
    path = directory / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise ResourceError("DOSDP bundle manifest is unavailable")
    try:
        allowance = min(operation.limits.max_bytes, operation.remaining("bytes"))
        with path.open("rb") as reader:
            raw = reader.read(allowance + 1)
    except OSError as error:
        raise ResourceError("DOSDP manifest cannot be read") from error
    if len(raw) > allowance:
        raise LimitExceeded("DOSDP manifest exceeds its byte allowance")
    operation.consume("bytes", len(raw))
    operation.consume("work", len(raw))
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ResourceError("DOSDP manifest differs from its explicit pin")
    manifest = object_value(
        decode_json(raw, max_bytes=operation.limits.max_bytes), location="DOSDP"
    )
    if set(manifest) != {"protocol", "dosdp", "main_class", "jars"} or (
        type(manifest["protocol"]) is not int
        or manifest["protocol"] != 1
        or manifest["dosdp"] != DOSDP_VERSION
        or manifest["main_class"] != MAIN_CLASS
    ):
        raise ResourceError("Unsupported DOSDP native bundle identity")
    entries = manifest["jars"]
    if type(entries) is not list or not entries:
        raise ResourceError("DOSDP requires a nonempty explicit ordered classpath")
    operation.consume("states", len(entries))
    expected: dict[str, str] = {}
    for raw_entry in entries:
        operation.consume("work")
        item = object_value(raw_entry, location="DOSDP jar")
        if set(item) != {"name", "sha256"}:
            raise ResourceError("Invalid DOSDP jar manifest entry")
        name, digest = item["name"], item["sha256"]
        if type(name) is not str or type(digest) is not str:
            raise ResourceError("DOSDP jar identities require ordinary text")
        bound_text(name, operation)
        bound_text(digest, operation)
        if (
            not name.endswith(".jar")
            or Path(name).name != name
            or name in expected
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ResourceError("Invalid or duplicated DOSDP jar identity")
        expected[name] = digest
    files = seal_files(directory, operation)
    actual = {item.name: item.sha256 for item in files}
    if actual != {"manifest.json": manifest_sha256, **expected}:
        raise ResourceError("DOSDP bundle inventory or jar content differs")
    return DosdpQueryBackend._owned(
        java=java,
        directory=directory,
        jars=tuple(expected),
        files=files,
        manifest_sha256=manifest_sha256,
        artifacts=artifacts.resolve(),
        version=DOSDP_VERSION,
    )
