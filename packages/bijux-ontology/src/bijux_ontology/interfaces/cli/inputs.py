"""Bounded console inputs and intentional output destinations."""

import argparse
import sys
from pathlib import Path

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.json.values import decode_json, object_value, read_json
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources.types import RdfSource


class UnsafeOutput(InputError):
    """An output aliases an input and must never receive even an error report."""


def json_input(path: Path, limits: Limits) -> object:
    """Read one explicit JSON snapshot or the sole requested stdin stream."""
    if str(path) == "-":
        return decode_json(
            sys.stdin.read(limits.max_bytes + 1),
            source="stdin",
            max_bytes=limits.max_bytes,
            max_depth=limits.max_depth,
        )
    return read_json(path, limits)


def source_map(path: Path | None, limits: Limits) -> dict[str, object]:
    """Resolve map filenames against that map, retaining logical resource keys."""
    if path is None:
        return {}
    if str(path) == "-":
        raise InputError(
            "Source maps require a filename to resolve their relative paths"
        )
    resources = object_value(json_input(path, limits), location=str(path))
    return {
        absolute_iri(iri): path.parent / document
        if isinstance(document, str)
        else document
        for iri, document in resources.items()
    }


def rdf_input(
    path: Path, *, format: str | None, base: str | None, limits: Limits
) -> RdfSource:
    """Keep physical files and logical RDF identity separate, including pipelines."""
    if str(path) == "-":
        if format is None or base is None:
            raise InputError("RDF stdin requires explicit --format and --base")
        return RdfSource(
            sys.stdin.read(limits.max_bytes + 1), format=format, document_iri=base
        )
    return RdfSource(path, format=format, document_iri=base)


def command_limits(options: argparse.Namespace) -> Limits:
    """Validate shared limit overrides without silently replacing zero values."""
    return Limits(
        **{
            key: value
            for key in Limits.__dataclass_fields__
            if (value := getattr(options, key, None)) is not None
        }
    )


def input_paths(
    options: argparse.Namespace, *, exclude: frozenset[str] = frozenset()
) -> tuple[Path, ...]:
    """Collect parser-declared paths before resource maps reveal more inputs."""
    paths = [
        value
        for key, value in vars(options).items()
        if key not in ("output", "artifact_root")
        and key not in exclude
        and isinstance(value, Path)
    ]
    for key, value in vars(options).items():
        if key not in exclude and isinstance(value, list):
            paths.extend(item for item in value if isinstance(item, Path))
    return tuple(paths)


def protect_paths(inputs: tuple[Path, ...], output: Path | None) -> None:
    """Reject competing stdin consumers and output aliases of known inputs."""
    if sum(str(path) == "-" for path in inputs) > 1:
        raise InputError("Only one explicitly identified input may read stdin")
    if output is not None:
        for path in inputs:
            if str(path) == "-":
                continue
            if (
                path.resolve() == output.resolve()
                or path.exists()
                and output.exists()
                and path.samefile(output)
            ):
                raise UnsafeOutput(
                    "Output must not overwrite an input", location=str(path)
                )


def protect_inputs(options: argparse.Namespace, extra: tuple[Path, ...] = ()) -> None:
    """Apply input protection to a parsed command and its discovered resources."""
    protect_paths(input_paths(options) + extra, options.output)


def write_output(content: str, path: Path | None) -> None:
    """Write only after checking; stdout remains free of progress messages."""
    if path is None:
        print(content)
    else:
        path.write_text(content + "\n", encoding="utf-8")
