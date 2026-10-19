"""Disposable maintained ShExJ compiler/evaluator with bounded binding receipts."""

import json
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from rdflib import Graph

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import Limits
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.rdf.entailment.protocol import decode_rows


def execute(request: dict[str, Any], directory: Path, cap: int) -> None:
    """Run only supplied inline schemas, RDF and exact named shape-map pairs."""
    budget = ControlBudget(Limits(**request["limits"]), request["allowances"])
    backend = ""
    status = "shex.engine_error"
    output_size = 0
    diagnostic: dict[str, str] | None = None

    def reserve(value: object) -> None:
        """Reserve JSON escaping and ownership before allocating retained output."""
        size = json_bound(value, budget) + 1024
        budget.consume("bytes", size)
        budget.consume("work", size)
        if size > cap:
            raise LimitExceeded("Native ShEx diagnostics exceed the protocol cap")

    def error_details(error: Exception) -> dict[str, str] | None:
        """Keep native exception fields only after their bounded retention is paid."""
        nonlocal status
        details = {"type": type(error).__name__, "message": str(error)}
        try:
            if len(details["message"]) > cap // 4:
                raise LimitExceeded("Native ShEx diagnostic exceeds its cap")
            reserve(details)
        except LimitExceeded:
            status = "shex.execution_incomplete"
            return None
        return details

    try:
        try:
            from pyrudof import (  # type: ignore[import-untyped]
                RDFFormat,
                ReaderMode,
                ResultShexValidationFormat,
                Rudof,
                RudofConfig,
                ShapeMapFormat,
                ShExFormat,
            )
        except ImportError:
            status = "shex.backend_unavailable"
            return
        if version("pyrudof") != "0.3.20":
            status = "shex.backend_unavailable"
            return
        backend = "pyrudof " + version("pyrudof")
        triples = decode_rows(request["data"], {}, budget, ordinary=True)
        graph = Graph(bind_namespaces="none")
        for triple in triples:
            budget.consume("work")
            graph.add(triple)
        # The existing term protocol preserves exact lexical forms and one shared
        # blank inventory. Reserve NTriples escaping before maintained formatting.
        size = json_bound(request["data"], budget)
        budget.consume("work", size)
        budget.consume("bytes", size)
        rdf = graph.serialize(format="nt") or "# Explicit supplied empty RDF graph\n"
        reserve(request["schema"])
        schema = json.dumps(request["schema"], ensure_ascii=False) + "\n"
        native = Rudof(RudofConfig())
        try:
            native.read_shex(
                schema, format=ShExFormat.ShExJ, reader_mode=ReaderMode.Strict
            )
        except ValueError as error:
            status = "shex.input_invalid"
            diagnostic = error_details(error)
            return
        native.read_data(rdf, format=RDFFormat.NTriples, reader_mode=ReaderMode.Strict)
        for index, pair in enumerate(request["bindings"]):
            budget.consume("work")
            if pair is None:
                continue
            focus, shape = pair
            native.reset_shapemap()
            native.read_shapemap(
                "<" + focus + ">@<" + shape + ">", format=ShapeMapFormat.Compact
            )
            # This owned progress record identifies an active native obligation;
            # it does not certify completion or supply a validation result.
            (directory / "binding-active").write_text(str(index), encoding="ascii")
            native.validate_shex()
            raw = native.serialize_shex_validation_results(
                format=ResultShexValidationFormat.Json
            )
            if len(raw) > cap:
                raise LimitExceeded("Native ShEx result exceeds the protocol cap")
            budget.consume("work", len(raw))
            budget.consume("bytes", len(raw.encode()))
            results = decode_json(raw, max_bytes=cap, max_depth=budget.limits.max_depth)
            if (
                not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], dict)
            ):
                raise InputError("Native ShEx result has unexpected binding coverage")
            result = results[0]
            info = result.get("appInfo")
            if (
                result.get("node") != focus
                or result.get("shape") != shape
                or result.get("status") not in ("conformant", "nonconformant")
                or not isinstance(info, dict)
                or info.get("status") != result["status"]
                or not isinstance(result.get("reason"), str)
            ):
                raise InputError("Native ShEx result cannot qualify this exact binding")
            record = {"index": index, "native": result, "backend": backend}
            reserve(record)
            record["consumed"] = budget.consumed()
            line = (
                json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
                + b"\n"
            )
            if output_size + len(line) > cap:
                raise LimitExceeded("Completed ShEx results exceed the protocol cap")
            with (directory / "completed.jsonl").open("ab") as handle:
                handle.write(line)
                handle.flush()
            output_size += len(line)
            (directory / "binding-active").unlink(missing_ok=True)
        status = "complete"
    except LimitExceeded:
        status = "shex.execution_incomplete"
    except Exception as error:
        status = "shex.engine_error"
        diagnostic = error_details(error)
    finally:
        terminal = {
            "status": status,
            "backend": backend,
            "consumed": budget.consumed(),
            "error": diagnostic,
        }
        payload = json.dumps(terminal, separators=(",", ":")).encode()
        if len(payload) <= cap:
            (directory / "result.json").write_bytes(payload)


def main() -> None:
    """Accept one bounded private request from the directly owning supervisor."""
    directory = Path(sys.argv[1])
    cap = int(sys.argv[2])
    with (directory / "request.json").open("rb") as handle:
        raw = handle.read(cap + 1)
    request = object_value(decode_json(raw, max_bytes=cap), location="ShEx request")
    execute(request, directory, cap)


if __name__ == "__main__":
    main()
