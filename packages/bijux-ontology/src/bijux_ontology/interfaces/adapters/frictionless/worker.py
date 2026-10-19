"""Actual optional Frictionless package work inside an owned process lifetime."""

import importlib
import json
import platform
import sys
from decimal import Decimal
from importlib.metadata import version
from typing import cast

from bijux_ontology._json import decode_json
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Budget, Limits, bound_text
from bijux_ontology.execution.protocol import json_bound

from .profile import array, object_value, text


def _value(value: object, budget: Budget) -> list[object]:
    """Preserve the native scalar type and exact Decimal value without JSON floats."""
    if value is None:
        return ["null", None]
    if type(value) is str:
        bound_text(value, budget)
        return ["string", value]
    if type(value) is bool:
        return ["boolean", value]
    if type(value) is int:
        budget.consume("bytes", 64 + value.bit_length())
        rendered = str(value)
        bound_text(rendered, budget)
        return ["integer", rendered]
    if isinstance(value, Decimal) and value.is_finite():
        budget.consume("bytes", 128 + 16 * value.__sizeof__())
        rendered = str(value)
        bound_text(rendered, budget)
        return ["decimal", rendered]
    raise ResourceError("Native rows contain an unsupported or nonfinite logical value")


def _append(
    target: list[object], value: object, budget: Budget, cap: int, size: list[int]
) -> None:
    """Bound each completed raw observation before it enters the reply prefix."""
    budget.consume("states")
    budget.consume("bytes", 128)
    measured = json_bound(value, budget)
    if size[0] + measured + 4096 > cap:
        raise LimitExceeded("Native package observations exceed the response cap")
    budget.consume("bytes", 4 * measured)
    budget.consume("work", measured)
    size[0] += measured
    target.append(value)


def _run(
    request: dict[str, object], reply: dict[str, object], budget: Budget, cap: int
) -> None:
    """Validate the unchanged original descriptor, then stream actual typed rows."""
    try:
        importlib.import_module("frictionless")
        importlib.import_module("frictionless.resources")
        Package = sys.modules["frictionless"].Package
        TableResource = sys.modules["frictionless.resources"].TableResource
    except ImportError as error:
        raise ResourceError(
            "Install bijux-ontology[frictionless] for native package reading"
        ) from error
    actual = version("frictionless")
    if actual != "5.21.0":
        raise ResourceError(
            "The qualified Frictionless profile requires version 5.21.0"
        )
    reply["producer"] = {
        "framework": actual,
        "python": platform.python_version(),
        "standard": "v1",
    }
    descriptor = object_value(request["descriptor"])
    package = Package(descriptor, basepath=text(request["directory"]))
    if Package.effective_datapackage_version(descriptor, None) != "v1":
        raise ResourceError("The original package is outside the qualified v1 regime")
    native = package.validate(
        limit_errors=cast(int, request["max_errors"]), parallel=False
    )
    errors = cast(list[object], reply["errors"])
    rows = cast(list[object], reply["rows"])
    warnings = cast(list[object], reply["warnings"])
    size = [4096]
    for native_error in native.errors:
        _append(
            errors,
            {"path": "", "error": native_error.to_descriptor()},
            budget,
            cap,
            size,
        )
    for warning in native.warnings:
        _append(warnings, {"path": "", "message": warning}, budget, cap, size)
    expected = array(descriptor["resources"])
    if len(native.tasks) != len(expected) or len(package.resources) != len(expected):
        raise ResourceError("Native validation did not cover every supplied table")
    for task, resource, raw in zip(
        native.tasks, package.resources, expected, strict=True
    ):
        item = object_value(raw)
        if (
            task.name != item["name"]
            or not isinstance(resource, TableResource)
            or resource.path != item["path"]
        ):
            raise ResourceError("Native package resource coverage is ambiguous")
        budget.consume("resources")
        for native_error in task.errors:
            _append(
                errors,
                {"path": item["path"], "error": native_error.to_descriptor()},
                budget,
                cap,
                size,
            )
        for warning in task.warnings:
            _append(
                warnings, {"path": item["path"], "message": warning}, budget, cap, size
            )
        fields = array(object_value(item["schema"])["fields"])
        names = [text(object_value(field)["name"]) for field in fields]
        with resource:
            for row in resource.row_stream:
                budget.consume("work")
                budget.consume("states", len(names) + 1)
                budget.consume("bytes", 256 + 128 * len(names))
                raw_cells = list(row.cells)
                values = [_value(row[name], budget) for name in names]
                _append(
                    rows,
                    {
                        "resource": item["name"],
                        "path": item["path"],
                        "row": row.row_number,
                        "raw": raw_cells,
                        "values": values,
                    },
                    budget,
                    cap,
                    size,
                )
    reply["valid"] = native.valid
    reply["complete"] = not warnings
    if native.valid and errors:
        raise ResourceError("Native validation returned inconsistent error coverage")
    if not native.valid and not errors:
        raise ResourceError("Native failure omitted its required diagnostic evidence")
    reply["status"] = "completed"


def process_request(payload: bytes, cap: int) -> bytes:
    """Return bounded original native evidence and exact explicit control counters."""
    budget = None
    reply: dict[str, object] = {
        "protocol": 1,
        "status": "failed",
        "producer": None,
        "valid": False,
        "complete": False,
        "warnings": [],
        "rows": [],
        "errors": [],
        "message": "",
        "controls": None,
    }
    try:
        request = object_value(decode_json(payload, max_bytes=cap))
        budget = ControlBudget(
            Limits(**cast(dict[str, int], object_value(request["limits"]))),
            request["allowances"],
        )
        budget.consume("bytes", 4 * json_bound(request, budget))
        output_cap = request["output_cap"]
        if type(output_cap) is not int or not 0 < output_cap <= cap:
            raise InputError("Invalid package output cap")
        maximum = request["max_errors"]
        if type(maximum) is not int or maximum <= 0:
            raise InputError("Invalid package diagnostic cap")
        _run(request, reply, budget, output_cap)
        size = json_bound(reply, budget) + 1024
        budget.consume("bytes", 4 * size)
        budget.consume("work", size)
    except Exception as error:
        reply["status"] = (
            "exhausted"
            if isinstance(error, LimitExceeded | MemoryError | RecursionError)
            else "unavailable"
            if isinstance(error, ResourceError)
            else "failed"
        )
        reply["valid"] = False
        reply["complete"] = False
        reply["message"] = str(error)[:1024] or type(error).__name__
    reply["controls"] = budget.consumed() if budget is not None else None
    raw = json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode()
    return raw if len(raw) <= cap else b"{}"


def main() -> int:
    """Read one capped request; the host owns cleanup, cancellation and reaping."""
    cap = int(sys.argv[1])
    sys.stdout.buffer.write(process_request(sys.stdin.buffer.read(cap + 1), cap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
