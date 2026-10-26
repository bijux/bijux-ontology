"""Finite exact-cell framing and maintained SSSOM production of authored tables."""

import csv
import io
import json
import time
from collections.abc import Iterator, Mapping, Sequence
from threading import Event
from typing import Literal

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import bound_text

from .interchange import read_sssom
from .types import MappingRead


def produce_mapping_table(
    columns: Sequence[str],
    rows: Sequence[Mapping[str, str]] | Iterator[Mapping[str, str]],
    *,
    metadata: dict[str, object],
    mapping_set_id: str,
    license: str,
    prefixes: dict[str, str],
    operation: Operation,
    framing: Literal["flow-map", "field-lines"],
    cancel: Event | None = None,
) -> MappingRead:
    """Produce and requalify exact ordered cells, never author selection authority.

    Both supported metadata framings are deliberately retained: the exposed
    MappingSet.source is observable, even when their YAML meaning is equal.
    The caller owns common interpretation, explicit approvals and cell meaning.
    The maintained reader alone qualifies the resulting table and native export.
    """
    if framing == "flow-map":
        header = (
            "# "
            + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            + "\n"
        )
        bound_text(header, operation)
        operation.consume("bytes", 4 * len(header))
        body = io.StringIO()
        writer = csv.writer(body, delimiter="\t", lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            values = []
            for column in columns:
                value = row.get(column, "")
                bound_text(value, operation)
                operation.consume("bytes", 4 * len(value) + 2)
                operation.consume("states")
                operation.consume("bytes", 8)
                values.append(value)
            writer.writerow(values)
        text = header + body.getvalue()
        bound_text(text, operation)
        operation.consume("bytes", 4 * len(text))
        operation.consume("work", 0)
        content = text.encode()
    elif framing == "field-lines":
        if not isinstance(rows, Sequence):
            raise InputError("Field-line table framing requires finite captured rows")
        # Fund the complete rectangular CSV and escaped metadata before writing.
        operation.consume("work", len(rows) * len(columns))
        operation.consume("states", len(rows) * len(columns))
        estimate = 4096 + 12 * (
            len(mapping_set_id)
            + len(license)
            + sum(len(k) + len(v) for k, v in prefixes.items())
            + sum(map(len, columns))
            + sum(sum(len(v) for v in row.values()) for row in rows)
            + len(rows) * len(columns)
        )
        operation.consume("bytes", estimate)
        operation.consume("work", estimate)
        stream = io.StringIO()
        for name, metadata_value in metadata.items():
            stream.write(
                "# "
                + name
                + ": "
                + json.dumps(metadata_value, ensure_ascii=True)
                + "\n"
            )
        dictionary_writer = csv.DictWriter(
            stream, fieldnames=columns, delimiter="\t", lineterminator="\n"
        )
        dictionary_writer.writeheader()
        dictionary_writer.writerows(rows)
        content = stream.getvalue().encode("utf-8")
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("Approved mapping source exceeds max_bytes")
    else:
        raise InputError("Unsupported exact-cell SSSOM metadata framing")
    return read_sssom(
        content,
        mapping_set_id=mapping_set_id,
        license=license,
        limits=operation.limits,
        budget=operation,
        timeout=max(0.001, operation.deadline - time.monotonic()),
        cancel=cancel,
    )
