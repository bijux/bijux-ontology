"""One command entry point for installed console and module invocation."""

import json
import sys
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.interfaces.cli.arguments import arguments
from bijux_ontology.interfaces.cli.checking import project, run_command
from bijux_ontology.interfaces.cli.execution import execute_bounded
from bijux_ontology.interfaces.cli.inputs import (
    UnsafeOutput,
    command_limits,
    protect_inputs,
    write_output,
)
from bijux_ontology.interfaces.cli.rendering import format_report
from bijux_ontology.interfaces.exports.sarif.conversion import to_sarif
from bijux_ontology.limits import Budget
from bijux_ontology.reports import (
    AcceptancePolicy,
    CheckError,
    Issue,
    IssueSelection,
    Location,
    Report,
    Severity,
)


def main(argv: list[str] | None = None) -> int:
    """Run supported checks with complete evidence and explicit process policy."""
    supplied = sys.argv[1:] if argv is None else argv
    options = arguments(supplied)
    policy = AcceptancePolicy()
    data: object = None
    configured = None
    try:
        policy = AcceptancePolicy(
            strict=options.strict,
            minimum_severity=cast(Severity, options.minimum_severity or "error"),
            exceptions=tuple(
                IssueSelection(code, location) for code, location in options.permit
            ),
        )
        protect_inputs(options)
        if options.display_limit is not None and options.display_limit < 0:
            options.display_limit = None
            raise InputError("display-limit must be nonnegative")
        if options.action == "project":
            configured = project(options)
            policy = configured.policy
            if options.explain:
                write_output(
                    json.dumps(configured.explain(), ensure_ascii=False, indent=2),
                    options.output,
                )
                return 0
        result = (
            run_command(options, configuration=configured)
            if options.timeout is None or options.action == "acquire"
            else execute_bounded(
                supplied,
                timeout=options.timeout,
                limits=configured.limits
                if configured is not None
                else command_limits(options),
                configuration=configured,
            )
        )
        report = result.report
        data = result.data
        if not result.output_safe:
            options.output = None
    except UnsafeOutput as error:
        options.output = None
        report = Report(
            options.action,
            issues=(Issue("input.output_alias", str(error), kind="input_error"),),
        )
    except CheckError as error:
        report = error.report
    except KeyboardInterrupt:
        report = Report(
            options.action,
            issues=(
                Issue(
                    "execution.cancelled", "Checking was interrupted", kind="exhausted"
                ),
            ),
        )
    except (OSError, UnicodeError, InputError) as error:
        options.output = None
        report = Report(
            options.action,
            issues=(
                Issue(
                    "execution.limit_exceeded"
                    if isinstance(error, LimitExceeded)
                    else "input.invalid",
                    str(error),
                    details=Location.from_error(error),
                    kind="exhausted"
                    if isinstance(error, LimitExceeded)
                    else "input_error",
                ),
            ),
        )
    sarif_content = None
    if options.sarif:
        try:
            export_budget = Budget(
                configured.limits if configured is not None else command_limits(options)
            )
            structured = to_sarif(
                report,
                artifact_root=options.artifact_root,
                policy=policy,
                budget=export_budget,
            )
            sarif_content = json.dumps(
                structured, ensure_ascii=False, separators=(",", ":")
            )
            size = len(sarif_content.encode("utf-8")) + 1
            if size > export_budget.limits.max_bytes:
                raise LimitExceeded("SARIF output exceeds max_bytes")
            export_budget.consume("work", len(sarif_content))
            export_budget.consume("bytes", size)
        except (InputError, UnicodeError):
            print(
                "SARIF export could not complete; native outcome=" + report.outcome,
                file=sys.stderr,
            )
            return 2
    else:
        structured = report.to_dict(policy=policy)
    if data is not None and not options.sarif:
        structured["data"] = data
    content = (
        sarif_content
        if sarif_content is not None
        else json.dumps(structured, ensure_ascii=False, indent=2)
        if options.json
        else (
            format_report(report, limit=options.display_limit, policy=policy)
            + (
                "\n" + json.dumps(data, ensure_ascii=False, indent=2)
                if data is not None
                else ""
            )
        )
    )
    try:
        protect_inputs(options)
        write_output(content, options.output)
    except (OSError, UnicodeError, InputError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return report.exit_code(policy=policy)
