"""Capture project input files once without changing their logical RDF identity."""

import json
from collections.abc import Mapping
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, BudgetKind, bound_text
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.projects.jobs import check_kind
from bijux_ontology.workflows.projects.resources import _CapturedFiles, _read_snapshot

KINDS: tuple[BudgetKind, ...] = (
    "bytes",
    "resources",
    "triples",
    "work",
    "issues",
    "states",
)


def supported(job: Mapping[str, object]) -> bool:
    """The isolated finite profile uses ordinary single-document validators."""
    try:
        kind = check_kind(job)
    except InputError:
        return False
    return kind in ("vocabulary", "shacl") or (
        kind == "context" and "documents" not in job
    )


def _metadata_bound(value: object, budget: Budget) -> int:
    """Prepay existing owned JSON metadata before copying or encoding it."""
    pending = [value]
    size = 0
    while pending:
        item = pending.pop()
        budget.consume("work")
        if isinstance(item, str):
            bound_text(item, budget)
            size += 2 + 12 * len(item)
        elif isinstance(item, dict):
            budget.consume("states", 2 * len(item))
            size += 2 + 2 * len(item)
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            budget.consume("states", len(item))
            size += 2 + len(item)
            pending.extend(item)
        elif type(item) is int:
            size += 4 + item.bit_length()
        elif item is None or type(item) in (bool, float):
            size += 32
        else:
            raise InputError("Expected already validated project JSON")
    return size


class _CapturedProjectConfig(ProjectConfig):
    """Validate the ordinary schema while preserving captured absolute paths."""

    def __init__(
        self, data: object, *, directory: Path, source: str, paths: frozenset[Path]
    ) -> None:
        self._paths = paths
        super().__init__(data, directory=directory, source=source)

    def path(self, value: object, *, location: str = "") -> Path:
        if not isinstance(value, str):
            raise InputError("Captured input requires a path", location=location)
        path = Path(value)
        if not path.is_absolute() or path not in self._paths:
            raise InputError("Input was not captured", location=location)
        return path


def capture(config: ProjectConfig, directory: Path, budget: Budget) -> Path:
    """Own immutable bytes and lazy per-file failure descriptors before dispatch.

    The manifest retains original paths and the original validated project.
    Worker resources replace only byte acquisition, preserving format inference,
    document/effective IRIs, compression and pinned graph selections.
    """
    budget.consume("resources")
    paths = config.input_paths()
    budget.consume("states", len(paths))
    manifest: dict[str, object] = {}
    for index, path in enumerate(paths):
        budget.consume("resources")
        budget.consume("work")
        bound_text(str(path), budget)
        try:
            value = _read_snapshot(path, budget)
        except OSError:
            manifest[str(path)] = None
            continue
        if len(value) > budget.limits.max_bytes:
            # An unusable file is diagnosed by its own job, not a sibling.
            manifest[str(path)] = "oversized"
            continue
        target = directory / f"input-{index}"
        target.write_bytes(value)
        manifest[str(path)] = target.name
    payload = {
        "configuration": config._data,
        "directory": str(config.directory),
        "source": config.source,
        "files": manifest,
    }
    bound = _metadata_bound(payload, budget)
    budget.consume("bytes", bound)
    resolved = config.explain()
    resolved.pop("source")
    resolved.pop("directory")
    resolved["version"] = config.version
    payload["configuration"] = resolved
    budget.consume("bytes", _metadata_bound(resolved, budget))
    # Existing strict configuration owns JSON. Bound its complete transport before
    # any workers receive it; a manifest is never a second project grammar.
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > budget.limits.max_bytes:
        raise LimitExceeded("Captured project metadata exceeds max_bytes")
    budget.consume("bytes", len(encoded))
    budget.consume("work", len(encoded))
    path = directory / "project.json"
    path.write_bytes(encoded)
    return path


def read_capture(
    path: Path, budget: Budget
) -> tuple[ProjectConfig, Mapping[Path, bytes | Exception]]:
    """Read only private captures; absent entries never reopen original files."""
    from bijux_ontology._json import _decode_json

    encoded = _read_snapshot(path, budget)
    value = _decode_json(
        encoded,
        max_bytes=budget.limits.max_bytes,
        max_depth=budget.limits.max_depth,
        budget=budget,
    )
    if not isinstance(value, dict) or not isinstance(value.get("files"), dict):
        raise InputError("Invalid captured project manifest")
    directory, source = value.get("directory"), value.get("source")
    if not isinstance(directory, str) or not isinstance(source, str):
        raise InputError("Invalid captured project identity")
    captured: dict[Path, Path | Exception] = {}
    for original, filename in value["files"].items():
        budget.consume("states")
        if not isinstance(original, str):
            raise InputError("Invalid captured input path")
        bound_text(original, budget)
        if filename is None:
            captured[Path(original)] = InputError(
                "Project input could not be read", location=original
            )
        elif filename == "oversized":
            captured[Path(original)] = LimitExceeded(
                "Project input exceeds max_bytes", location=original
            )
        elif isinstance(filename, str) and Path(filename).name == filename:
            captured[Path(original)] = path.parent / filename
        else:
            raise InputError("Invalid private capture filename")
    budget.consume("states", len(captured))
    config = _CapturedProjectConfig(
        value.get("configuration"),
        directory=Path(directory),
        source=source,
        paths=frozenset(captured),
    )
    return config, _CapturedFiles(captured, budget)
