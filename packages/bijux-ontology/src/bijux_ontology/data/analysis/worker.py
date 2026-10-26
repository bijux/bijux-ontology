"""Concrete isolated SciPy exact testing and complete-family FDR correction."""

import json
import math
import sys
from importlib.metadata import version
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Limits, bound_text
from bijux_ontology.execution.versions import qualify_release
from bijux_ontology.json.values import decode_json, object_value


def calculate(request: dict[str, object], capacity: int) -> dict[str, object]:
    """Validate the concrete finite tables before entering maintained numerical code."""
    if set(request) - {"required_version"} != {
        "tables",
        "correction",
        "limits",
        "allowances",
    }:
        raise InputError("Malformed annotation analysis request")
    limits_data = object_value(request["limits"], location="/limits")
    if any(type(value) is not int for value in limits_data.values()):
        raise InputError("Analysis limits require integers")
    limits = Limits(**cast(dict[str, int], limits_data))
    budget = ControlBudget(limits, request["allowances"])
    result: dict[str, object]
    try:
        values = request["tables"]
        correction = request["correction"]
        if type(values) is not list or not values or correction not in ("by", "bh"):
            raise InputError(
                "Analysis requires a finite nonempty table family and correction"
            )
        budget.consume("states", 12 * len(values))
        budget.consume("bytes", 512 * len(values))
        tables = []
        for value in values:
            budget.consume("work", 16)
            if type(value) is not list or len(value) != 2:
                raise InputError("Exact testing requires two-by-two tables")
            rows = []
            for row in value:
                if type(row) is not list or len(row) != 2:
                    raise InputError("Exact testing requires two-by-two tables")
                if any(
                    type(count) is not int or not 0 <= count <= 2**63 - 1
                    for count in row
                ):
                    raise InputError(
                        "Exact counts exceed the qualified native signed-integer range"
                    )
                rows.append(row)
            tables.append(rows)
        try:
            import scipy  # type: ignore[import-untyped]
            from scipy.stats import (  # type: ignore[import-untyped]
                false_discovery_control,
                fisher_exact,
            )
        except ModuleNotFoundError:
            result = {
                "status": "unavailable",
                "message": "Install bijux-ontology[analysis] "
                "for maintained exact testing",
            }
        else:
            try:
                qualify_release(
                    "scipy",
                    scipy.__version__,
                    cast(str | None, request.get("required_version")),
                )
            except ResourceError as error:
                result = {"status": "unsupported", "message": str(error)}
            else:
                budget.consume("work", 8 * len(tables))
                raw = [
                    float(
                        fisher_exact(table, alternative="greater", method=None).pvalue
                    )
                    for table in tables
                ]
                adjusted = [
                    float(value)
                    for value in false_discovery_control(raw, method=correction)
                ]
                if any(
                    not math.isfinite(value) or not 0 <= value <= 1
                    for value in (*raw, *adjusted)
                ):
                    raise InputError(
                        "Maintained numerical output is not a finite probability"
                    )
                bound_text(scipy.__version__, budget)
                numpy_version = version("numpy")
                bound_text(numpy_version, budget)
                result = {
                    "status": "complete",
                    "raw": raw,
                    "adjusted": adjusted,
                    "scipy": scipy.__version__,
                    "numpy": numpy_version,
                }
        budget.consume("bytes", capacity * 2)
        budget.consume("work", capacity)
    except LimitExceeded:
        result = {
            "status": "exhausted",
            "message": "Numerical worker exhausted its shared control allowance",
        }
    result["consumed"] = budget.consumed()
    payload = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(payload) > capacity:
        raise LimitExceeded("Analysis response exceeds its owned protocol bound")
    return result


def main() -> None:
    """Read one bounded request and publish one complete native family response."""
    capacity = int(sys.argv[1])
    if capacity <= 0:
        raise InputError("Numerical worker requires a positive byte capacity")
    payload = sys.stdin.buffer.read(capacity + 1)
    if len(payload) > capacity:
        raise LimitExceeded("Analysis request exceeds its owned protocol bound")
    request = object_value(
        decode_json(payload, max_bytes=capacity, max_depth=16), location="/"
    )
    result = calculate(request, capacity)
    sys.stdout.buffer.write(
        json.dumps(result, allow_nan=False, separators=(",", ":")).encode("ascii")
    )


if __name__ == "__main__":
    main()
