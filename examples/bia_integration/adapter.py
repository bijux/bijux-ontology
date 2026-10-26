"""Reusable native BIA output checks; biological validation remains with BIA."""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from bia_ro_crate.core.bia_ro_crate_metadata import BIAROCrateMetadata

from bijux_ontology.declarations.bindings import (
    BindingIdentity,
    BindingObservation,
    ContractEvidence,
)
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import combine_reports, report_from_data
from bijux_ontology.interfaces.execution.executor import ExecutionResult, WorkerExecutor
from bijux_ontology.workflows.projects.contracts import (
    ContractObligation,
    ContractResult,
    check_contract,
)


@dataclass(frozen=True)
class NativeOutputCheck:
    """Raw finite sample evidence beside an independently required field policy."""

    document: dict[str, object]
    execution: ExecutionResult
    bindings: tuple[BindingObservation, ...]
    required: ContractResult | None
    unassociated_models: tuple[str, ...]


def check_metadata(
    metadata: BIAROCrateMetadata,
    models: Sequence[ModelDeclaration],
    *,
    executor: WorkerExecutor,
    vocabulary: str,
    required_bindings: tuple[BindingIdentity, ...] = (),
) -> NativeOutputCheck:
    """Call the real producer once; do not replace BIA parser/business validation."""
    return check_output(
        metadata,
        metadata.to_dict(),
        models,
        executor=executor,
        vocabulary=vocabulary,
        required_bindings=required_bindings,
    )


def check_output(
    metadata: BIAROCrateMetadata,
    document: dict[str, object],
    models: Sequence[ModelDeclaration],
    *,
    executor: WorkerExecutor,
    vocabulary: str,
    required_bindings: tuple[BindingIdentity, ...] = (),
) -> NativeOutputCheck:
    """Check original native output with explicit, independent intended mappings.

    The metadata objects associate application classes and authored record IDs;
    the worker must prove each source/entity correspondence. No fixture revision,
    model count, scalar guarantee or required field is inferred here. Output must
    already have passed the caller's BIA business checks.
    """
    intended = tuple(models)
    registry = {model.identifier: model for model in intended}
    records = document.get("@graph")
    if not isinstance(records, list):
        raise InputError("Native BIA output requires its original @graph array")
    entities = {
        entity.id: f"{type(entity).__module__}.{type(entity).__qualname__}"
        for entity in metadata.get_objects()
    }
    usages, selections = [], []
    associated = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise InputError("Native BIA @graph records require objects")
        identity = record.get("@id")
        name = entities.get(identity)
        if name is None or name not in registry:
            raise InputError("An emitted entity has no caller-supplied model mapping")
        if identity in associated:
            raise InputError("An emitted native entity has duplicate authored records")
        associated.add(identity)
        usage = {"model": name, "path": ["@graph", index], "entity": True}
        usages.append(usage)
        for field in registry[name].fields:
            selections.append(
                {
                    "identity": {"owner": name, "path": [identity, field.name]},
                    "usage": usage,
                    "field": field.name,
                    "path": [
                        "@graph",
                        index,
                        field.key if field.key is not None else field.name,
                    ],
                }
            )
    base = (metadata.get_base_path() / metadata.DEFAULT_RO_CRATE_FILENAME).as_uri()
    execution = executor.run(
        "emission",
        {
            "document": document,
            "base": base,
            "models": json.loads(json.dumps([asdict(model) for model in intended])),
            "usages": usages,
            "bindings": selections,
            "vocabulary": {"source": vocabulary, "format": "turtle"},
        },
    )
    observations = []
    if isinstance(execution.data, dict):
        for item in execution.data.get("bindings", []):
            observations.append(
                BindingObservation(
                    BindingIdentity(item["owner"], tuple(item["path"])),
                    report_from_data(item["report"]),
                    item["exclusion"],
                )
            )
    required = None
    if required_bindings:
        selected = tuple(
            item for item in observations if item.identity in required_bindings
        )
        reports = [item.report for item in selected]
        failures = tuple(
            issue
            for issue in execution.report
            if issue.kind in {"input_error", "exhausted"}
        )
        if failures:
            # A selected semantic proof cannot hide failure of its execution boundary.
            reports.append(
                replace(execution.report, issues=failures, children=(), complete=False)
            )
        evidence = ContractEvidence(
            combine_reports("bia.required_fields", *reports),
            selected,
        )
        required = check_contract(
            (
                ContractObligation(
                    "native-fields",
                    lambda: evidence,
                    required_bindings=required_bindings,
                ),
            )
        )
    return NativeOutputCheck(
        document,
        execution,
        tuple(observations),
        required,
        tuple(sorted(set(registry) - {entities[item] for item in associated})),
    )
