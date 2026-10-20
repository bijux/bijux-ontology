"""Explicit preparation command using the ordinary bounded acquisition API."""

import argparse
from dataclasses import asdict
from pathlib import Path

from bijux_ontology.documents.archive import bundle_reader
from bijux_ontology.interfaces.cli.inputs import command_limits, protect_inputs
from bijux_ontology.interfaces.cli.results import CommandResult
from bijux_ontology.json.values import decode_json
from bijux_ontology.workflows.projects.acquisition.preparation import acquire_bundle
from bijux_ontology.workflows.projects.acquisition.types import (
    AcquisitionPolicy,
    AcquisitionRequest,
)
from bijux_ontology.workflows.projects.configuration import ProjectConfig


def acquire(options: argparse.Namespace) -> CommandResult:
    """Prepare selected resources and retain raw availability evidence.

    The API owns the isolated network/processor worker and its deadline. The CLI
    does not nest another process around it or turn ordinary checks into fetches.
    """
    limits = command_limits(options)
    source: Path = options.source
    directory = source.resolve() if source.is_dir() else Path.cwd()
    with bundle_reader(source, limits=limits) as read:
        configuration = ProjectConfig(
            decode_json(
                read("project.json"),
                max_bytes=limits.max_bytes,
                max_depth=limits.max_depth,
            ),
            directory=directory,
        )
    if source.is_dir():
        protect_inputs(
            options, configuration.input_paths() + (source / "project.json",)
        )
    result = acquire_bundle(
        source,
        requests=tuple(AcquisitionRequest(path, url) for path, url in options.request),
        policy=AcquisitionPolicy(
            tuple(options.origin),
            max_redirects=options.max_redirects,
            max_requests=options.max_requests,
            max_compressed_bytes=options.max_compressed_bytes,
        ),
        destination=options.destination,
        limits=limits,
        timeout=30.0 if options.timeout is None else options.timeout,
    )
    return CommandResult(
        result.report,
        {
            "archive": None if result.archive is None else str(result.archive),
            "resources": [asdict(resource) for resource in result.resources],
        },
    )
