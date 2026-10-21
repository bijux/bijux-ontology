"""Discover only declared logical job filenames, preserving lazy per-job errors."""

from collections.abc import Mapping

from bijux_ontology.diagnostics.errors import InputError

from .descriptors import describe


def input_paths(
    job: Mapping[str, object],
) -> tuple[tuple[tuple[str | int, ...], object], ...]:
    """Inspect declared filename slots without opening or interpreting files."""
    try:
        descriptor = describe(str(job.get("kind")))
    except InputError:
        return ()
    paths: list[tuple[tuple[str | int, ...], object]] = []
    for key in descriptor.direct_files:
        if key in job:
            paths.append(((key,), job[key]))
    if descriptor.document_files:
        members = job.get("documents", [])
        if not isinstance(members, list):
            members = []
        for index, member in enumerate(members):
            if not isinstance(member, dict):
                continue
            for key in descriptor.document_files:
                if key in member:
                    paths.append((("documents", index, key), member[key]))
    for role in descriptor.context_files:
        contexts = job.get(role, {})
        if not isinstance(contexts, dict):
            continue
        for iri, value in contexts.items():
            paths.append(((role, iri), value))
    for side in descriptor.application_sides:
        entry = job.get(side)
        if isinstance(entry, dict) and "application" in entry:
            paths.append(((side, "application"), entry["application"]))
    return tuple(
        (path, value)
        for path, value in paths
        if isinstance(value, str) and value.strip()
    )
