"""Capture exact original closure revisions without starting a JVM."""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Any, cast

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import Payload, RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report

from ...backend.owlapi.bundles import LOADER_PROFILE, qualify_backend
from ...backend.types import OwlProfileBackend, ProfileDocument
from ...documents.sources.native import stage_native_documents
from ...documents.sources.rdf import capture_rdf_closure, stage_documents
from ...documents.types import OwlDocument
from .types import PreparedReasoningTheory, TheoryDocument


def encode_request(value: dict[str, Any], budget: Budget) -> bytes:
    """Prepay actual finite JSON serialization before constructing the payload."""
    budget.consume("bytes", json_bound(value, budget))
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > budget.limits.max_bytes:
        raise LimitExceeded("Reasoning session request exceeds max_bytes")
    return encoded


def prepare_reasoning_theory(
    backend: OwlProfileBackend,
    source: OwlDocument | PreparedSource,
    *,
    imports: Mapping[str, OwlDocument | RdfSource | Payload] | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> PreparedReasoningTheory:
    """Capture a fixed input revision using the existing native closure contracts.

    Captured imports are independent of subsequent external map/file changes.
    Prepare and bind another theory to request a new revision. Backend sealed
    files are additionally rechecked by every session request.
    """
    operation = Operation(limits, budget, timeout, cancel)
    qualify_backend(backend, operation)
    issues = IssueCollector(operation)
    reports: list[Report] = []
    rdf_sources: tuple[PreparedSource, ...] = ()
    backend.artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="theory-capture-", dir=backend.artifacts) as job:
        directory = Path(job)
        originals: tuple[OwlDocument | ProfileDocument, ...]
        if type(source) is OwlDocument:
            originals, request = stage_native_documents(
                source,
                cast(Mapping[str, OwlDocument] | None, imports),
                directory,
                operation,
            )
            kind = "native"
        elif type(source) is PreparedSource:
            closure = capture_rdf_closure(
                source,
                cast(Mapping[str, RdfSource | Payload] | None, imports),
                operation,
                reports=reports,
            )
            rdf_sources = closure.originals
            originals, request, _ = stage_documents(
                closure.source,
                closure.imports,
                directory,
                operation,
                issues,
                reports,
            )
            kind = "rdf"
        else:
            raise InputError(
                "Reasoning theory needs an ordinary owned RDF/native source"
            )
        if issues or any(not report.verified for report in reports):
            raise ResourceError("Captured reasoning closure is unqualified")
        entries = request["documents"]
        operation.consume("states", len(entries) * 4)
        operation.consume("bytes", 256 + len(entries) * 256)
        files = []
        digest = hashlib.sha256()
        metadata: list[dict[str, Any]] = []
        for record in entries:
            operation.consume("work")
            path = Path(record["path"])
            size = path.stat().st_size
            if size > operation.limits.max_bytes:
                raise LimitExceeded("Captured reasoning document exceeds max_bytes")
            operation.consume("bytes", size + 128)
            operation.consume("work", size + 1)
            with path.open("rb") as reader:
                content = reader.read(size + 1)
            if len(content) != size:
                raise ResourceError(
                    "Captured reasoning document changed during reading"
                )
            fields = []
            for key, value in record.items():
                if key == "path":
                    continue
                bound_text(key, operation)
                if value is not None:
                    if type(value) is not str:
                        raise ResourceError("Unexpected native staging metadata")
                    bound_text(value, operation)
                operation.consume("states")
                fields.append((key, value))
            files.append(TheoryDocument(content, tuple(fields)))
            metadata.append(
                {"fields": fields, "sha256": hashlib.sha256(content).hexdigest()}
            )
        aliases = request["aliases"]
        operation.consume("states", len(aliases) * 2)
        operation.consume("bytes", 128 + 64 * len(aliases))
        for alias in aliases:
            bound_text(alias, operation)
        identity = {
            "kind": kind,
            "files": metadata,
            "aliases": sorted(aliases.items()),
            "backend": backend.manifest_sha256,
            "java": str(backend.java),
            "loader": LOADER_PROFILE,
            "reasoner": ["HermiT", "1.4.5.519", "1.4.1.513", False],
        }
        digest.update(encode_request(identity, operation))
        return PreparedReasoningTheory._owned(
            backend=backend,
            identity=digest.hexdigest(),
            input_kind=kind,
            documents=originals,
            rdf_sources=rdf_sources,
            aliases=tuple(sorted(aliases.items())),
            files=tuple(files),
            report=Report(
                "owl.reasoning_theory",
                len(files),
                requested=len(files),
                children=tuple(reports),
                regime="exact-captured-revision",
            ),
        )


def stage_theory(
    theory: PreparedReasoningTheory, directory: Path, budget: Budget
) -> dict[str, Any]:
    """Write only captured bytes for the worker's fixed original closure."""
    if type(theory) is not PreparedReasoningTheory or not theory.report.verified:
        raise InputError("Session binding requires a qualified captured theory")
    budget.consume("states", 4 * len(theory.files) + len(theory.aliases))
    budget.consume("bytes", 256 + 256 * len(theory.files) + 64 * len(theory.aliases))
    entries = []
    for index, document in enumerate(theory.files):
        budget.consume("work", len(document.content) + 1)
        budget.consume("bytes", len(document.content))
        path = directory / f"document-{index}.owl"
        bound_text(str(path), budget)
        with path.open("xb") as writer:
            writer.write(document.content)
        entries.append({"path": str(path), **dict(document.metadata)})
    return {
        "documents": entries,
        "aliases": dict(theory.aliases),
        "root": 0,
        "input_kind": theory.input_kind,
        "theory_identity": theory.identity,
    }
