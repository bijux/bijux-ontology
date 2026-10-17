"""Owned expensive command execution; ordinary API calls stay synchronous."""

import json
import sys
from pathlib import Path

from bijux_ontology._json import object_value, read_json, text_value
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.cli.arguments import arguments
from bijux_ontology.interfaces.cli.checking import run_command
from bijux_ontology.interfaces.cli.checkpoints import retain_completed
from bijux_ontology.interfaces.cli.inputs import command_limits
from bijux_ontology.reports import CheckError, Issue, Report
from bijux_ontology.workflows.projects.configuration import ProjectConfig


def execute(request_path: Path) -> None:
    """Write completed evidence before final output; crashes remain failures."""
    request = object_value(read_json(request_path), location=str(request_path))
    argv = request.get("argv")
    if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
        raise InputError("Worker arguments must be strings")
    options = arguments(argv)
    destination = request_path.parent
    configured = request.get("project")
    configuration = None
    if configured is not None:
        selected = object_value(configured, location="project")
        configuration = ProjectConfig(
            selected.get("data"),
            directory=Path(text_value(selected.get("directory"), location="directory")),
            source=text_value(selected.get("source"), location="source"),
        )
    limits = (
        configuration.limits if configuration is not None else command_limits(options)
    )

    def completed(report: Report) -> None:
        """Flush each finished required scope so a deadline retains its findings."""
        retain_completed(report, destination, max_bytes=limits.max_total_bytes)

    data: object = None
    output_safe = True
    try:
        result = run_command(options, completed=completed, configuration=configuration)
        report = result.report
        data = result.data
        output_safe = result.output_safe
    except CheckError as error:
        report = error.report
    except (OSError, UnicodeError, InputError) as error:
        output_safe = False
        report = Report(
            options.action,
            issues=(
                Issue(
                    "execution.limit_exceeded"
                    if isinstance(error, LimitExceeded)
                    else "input.invalid",
                    str(error),
                    kind="exhausted"
                    if isinstance(error, LimitExceeded)
                    else "input_error",
                ),
            ),
        )
    encoded = json.dumps(
        {"report": report.to_dict(), "data": data, "output_safe": output_safe},
        ensure_ascii=False,
    ).encode("utf-8")
    if len(encoded) > limits.max_total_bytes:
        (destination / "result-exhausted").touch()
    else:
        (destination / "result.json").write_bytes(encoded)


if __name__ == "__main__":
    execute(Path(sys.argv[1]))
