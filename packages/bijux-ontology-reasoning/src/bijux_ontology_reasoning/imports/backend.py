"""One fixed native MIREOT dialect over exclusively owned staged files."""

from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.errors import ResourceError
from bijux_ontology.execution.budgets import Operation

from ..robot.runtime import _verify, run_robot
from ..robot.types import RobotBackend, RobotRun
from .preparation import MireotPlan


def extract_native(
    runtime: RobotBackend, plan: MireotPlan, operation: Operation, heap_mb: int
) -> RobotRun:
    """Run maintained extraction/annotation/conversion without user CLI fragments."""
    _verify(runtime, operation)
    runtime.artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="selective-import-", dir=runtime.artifacts) as job:
        directory = Path(job)
        source = directory / "original.ofn"
        native, rdf = directory / "module.ofn", directory / "module.ttl"
        operation.consume("bytes", len(plan.source.content))
        source.write_bytes(plan.source.content)
        arguments = ["extract", "--method", "MIREOT", "--input", str(source)]
        operation.consume("bytes", 256 + len(plan.boundaries) * 256)
        for boundary in plan.boundaries:
            arguments.extend(
                ("--lower-term", boundary.lower.iri, "--upper-term", boundary.upper.iri)
            )
        arguments.extend(
            (
                "--intermediates",
                "all",
                "--copy-ontology-annotations",
                "true" if plan.retention.ontology_annotations == "copy" else "false",
                "--annotate-with-source",
                "false",
                "annotate",
                "--ontology-iri",
                plan.module_iri,
            )
        )
        if plan.module_version_iri is not None:
            arguments.extend(("--version-iri", plan.module_version_iri))
        arguments.extend(
            (
                "--output",
                str(native),
                "convert",
                "--format",
                "ttl",
                "--output",
                str(rdf),
            )
        )
        result = run_robot(runtime, tuple(arguments), (native, rdf), operation, heap_mb)
        if result.returncode != 0 or any(output is None for output in result.outputs):
            operation.consume("bytes", 256 + 4 * len(result.diagnostic))
            raise ResourceError(
                "Native MIREOT did not produce both ordinary outputs: "
                + result.diagnostic
            )
        return result
