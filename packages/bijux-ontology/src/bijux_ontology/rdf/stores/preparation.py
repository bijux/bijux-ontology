"""Persistent owned native snapshots with restart qualification and content seals."""

import hashlib
import json
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from rdflib import BNode, URIRef

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import Budget, Limits, bound_text
from bijux_ontology.rdf.queries.protocol import read_term

from .protocol import execute, failure
from .seals import StoreFile, seal_files
from .types import StoredRdf, StoredRdfResult


def _adopt(
    directory: Path,
    metadata: dict[str, Any],
    files: tuple[StoreFile, ...],
    operation: Operation,
) -> StoredRdf:
    """Keep native blank graph IDs private to newly owned public selector objects."""
    if set(metadata) != {
        "source_sha256",
        "source_format",
        "quads",
        "graphs",
        "backend",
        "files",
    }:
        raise ResourceError("Unsupported disk snapshot manifest fields")
    if (
        metadata["source_format"] not in ("nt", "nquads")
        or type(metadata["quads"]) is not int
        or metadata["quads"] < 0
    ):
        raise ResourceError("Invalid disk snapshot source metadata")
    for key in ("source_sha256", "backend"):
        if not isinstance(metadata[key], str) or not metadata[key]:
            raise ResourceError("Missing disk snapshot identity metadata")
        bound_text(metadata[key], operation)
    if len(metadata["source_sha256"]) != 64 or any(
        c not in "0123456789abcdef" for c in metadata["source_sha256"]
    ):
        raise ResourceError("Invalid disk snapshot source hash")
    if not isinstance(metadata["graphs"], list):
        raise ResourceError("Invalid disk snapshot graph inventory")
    names = []
    for value in metadata["graphs"]:
        operation.consume("states")
        operation.consume("resources")
        node = read_term(value, {})
        if not isinstance(node, (URIRef, BNode)):
            raise ResourceError("Invalid native graph resource")
        names.append(BNode(value["value"]) if isinstance(node, BNode) else node)
    if len(set(names)) != len(names):
        raise ResourceError("Duplicate native graph identity")
    raw = json.dumps(
        metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode()
    operation.consume("bytes", len(raw))
    return StoredRdf._owned(
        directory=directory,
        files=files,
        source_sha256=metadata["source_sha256"],
        source_format=metadata["source_format"],
        quads=metadata["quads"],
        graph_names=tuple(names),
        graph_fidelity="nonempty-only",
        backend_version=metadata["backend"],
        revision=hashlib.sha256(raw).hexdigest(),
    )


def snapshot_rdf(
    source: Path,
    destination: Path,
    *,
    format: str = "nt",
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 60,
    cancel: threading.Event | None = None,
) -> StoredRdfResult:
    """Create a closed immutable native backup from a captured local RDF stream.

    The finite input profile is ordinary N-Triples/N-Quads whose exact terms the
    maintained backend preserves. Noncanonical numeric lexicals are unsupported,
    not rewritten. Native storage and reads never allocate a whole RDFLib graph.
    Destination must be absent; failed builds remove only this operation's owner.
    """
    operation = Operation(limits, budget, timeout, cancel)
    created = False
    try:
        if (
            not isinstance(source, Path)
            or not isinstance(destination, Path)
            or format not in ("nt", "nquads")
        ):
            raise InputError("Disk snapshots require local Paths and nt/nquads format")
        destination.mkdir()
        created = True
        with TemporaryDirectory(prefix="capture-", dir=destination) as private:
            captured = Path(private) / "source.rdf"
            before = source.stat()
            digest = hashlib.sha256()
            size = 0
            with source.open("rb") as reader, captured.open("xb") as writer:
                while chunk := reader.read(65536):
                    operation.consume("work")
                    size += len(chunk)
                    if size > operation.limits.max_bytes:
                        raise LimitExceeded("RDF source exceeds max_bytes")
                    operation.consume("bytes", len(chunk))
                    writer.write(chunk)
                    digest.update(chunk)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ino,
            ):
                raise ResourceError("RDF source changed during disk snapshot capture")
            response = execute(
                {
                    "operation": "build",
                    "source": str(captured.resolve()),
                    "format": format,
                    "mutable": str((Path(private) / "mutable").resolve()),
                    "database": str((destination / "database").resolve()),
                },
                operation,
            )
            if response.get("status") != "inventory":
                raise ResourceError(
                    str(response.get("message", "Native ingestion incomplete"))
                )
        files = seal_files(destination / "database", operation)
        metadata = {
            "source_sha256": digest.hexdigest(),
            "source_format": format,
            "quads": response["quads"],
            "graphs": response["graphs"],
            "backend": response["backend"],
            "files": [asdict(item) for item in files],
        }
        snapshot = _adopt(
            (destination / "database").resolve(), metadata, files, operation
        )
        encoded = json.dumps(
            metadata, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode()
        operation.consume("bytes", len(encoded))
        with (destination / "manifest.json").open("xb") as writer:
            writer.write(encoded)
        return StoredRdfResult(
            snapshot,
            Report("rdf.store", 1, requested=1, regime="native-exact-rdf-snapshot"),
        )
    except (InputError, OSError) as error:
        if created:
            shutil.rmtree(destination)
        typed = error if isinstance(error, InputError) else ResourceError(str(error))
        return StoredRdfResult(None, failure(typed, operation, "rdf.store"))


def open_snapshot(
    destination: Path,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 60,
    cancel: threading.Event | None = None,
) -> StoredRdfResult:
    """Requalify sealed file content and actual native inventory after restart."""
    operation = Operation(limits, budget, timeout, cancel)
    try:
        if not isinstance(destination, Path):
            raise InputError("Disk restart requires a snapshot Path")
        with (destination / "manifest.json").open("rb") as stream:
            raw = stream.read(operation.limits.max_bytes + 1)
        operation.consume("bytes", len(raw))
        metadata = decode_json(
            raw,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        if not isinstance(metadata, dict):
            raise ResourceError("Invalid disk snapshot manifest")
        files = seal_files(destination / "database", operation)
        if metadata.get("files") != [asdict(item) for item in files]:
            raise ResourceError("Disk snapshot content differs from its published seal")
        snapshot = _adopt(
            (destination / "database").resolve(), metadata, files, operation
        )
        inventory = execute({"operation": "inventory"}, operation, snapshot)
        if inventory.get("status") != "inventory" or any(
            inventory.get(key) != metadata[key]
            for key in ("quads", "graphs", "backend")
        ):
            raise ResourceError(
                "Disk snapshot manifest does not match native inventory"
            )
        return StoredRdfResult(
            snapshot,
            Report("rdf.store", 1, requested=1, regime="native-exact-rdf-snapshot"),
        )
    except (InputError, OSError, KeyError, TypeError) as error:
        typed = error if isinstance(error, InputError) else ResourceError(str(error))
        return StoredRdfResult(None, failure(typed, operation, "rdf.store"))
