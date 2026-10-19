"""Owned table-template staging over the shared sealed ROBOT runtime."""

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from bijux_ontology.execution.budgets import Operation

from ..robot.runtime import _verify, prepare_robot_backend, run_robot
from .types import TemplateBackend

prepare_template_backend = prepare_robot_backend


@dataclass(frozen=True)
class NativeTable:
    """Actual maintained table receipts with separate ontology and cell errors."""

    identity: str
    returncode: int | None
    diagnostic: str
    output: bytes | None
    errors: bytes | None


def execute_table(
    backend: TemplateBackend,
    table: bytes,
    bindings: bytes,
    suffix: str,
    prefixes: tuple[tuple[str, str], ...],
    ontology_iri: str,
    operation: Operation,
    heap_mb: int,
) -> NativeTable:
    """Stage the exact native template inputs and fixed documented CLI flags."""
    _verify(backend, operation)
    operation.consume("bytes", len(table) + len(bindings))
    backend.artifacts.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="table-template-", dir=backend.artifacts) as job:
        directory = Path(job)
        input_table, input_bindings, output, errors = (
            directory / name
            for name in (
                "table." + suffix,
                "bindings.ofn",
                "ontology.ofn",
                "errors.csv",
            )
        )
        input_table.write_bytes(table)
        input_bindings.write_bytes(bindings)
        arguments = [
            "template",
            "--input",
            str(input_bindings),
            "--ontology-iri",
            ontology_iri,
            "--template",
            str(input_table),
            "--errors",
            str(errors),
            "--output",
            str(output),
        ]
        for prefix, namespace in prefixes:
            arguments += ["--prefix", prefix + ": " + namespace]
        result = run_robot(
            backend, tuple(arguments), (output, errors), operation, heap_mb
        )
        return NativeTable(
            result.identity,
            result.returncode,
            result.diagnostic,
            result.outputs[0],
            result.outputs[1],
        )
