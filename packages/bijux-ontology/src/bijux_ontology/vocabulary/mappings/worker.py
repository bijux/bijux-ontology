"""Isolated maintained SSSOM parsing, schema validation and faithful TSV writing."""

import base64
import json
import logging
import socket
import sys
import warnings
from importlib.metadata import version
from io import StringIO
from typing import Any, cast

from bijux_ontology._json import decode_json, object_value, own_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.protocol import json_bound
from bijux_ontology.limits import Limits, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri


class _Unsupported(InputError):
    """A required lexical or semantic interchange condition was not qualified."""


class _Warnings(logging.Handler):
    """Reject processor warning states without interpreting diagnostic prose."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.seen = False

    def emit(self, record: logging.LogRecord) -> None:
        self.seen = True


def _offline(*args: object, **kwargs: object) -> Any:
    """No supplied stream or schema operation may acquire remote resources."""
    raise InputError("SSSOM interchange does not permit network access")


def _framing(text: str, operation: Operation) -> str:
    """Bound lexical framing only; YAML and TSV grammar remain maintained.

    The raw public Pandas reader treats '#' as a comment marker. Reject it in
    the physical table body, including quoted cells, before that reader can
    discard data. Leading SSSOM comments are interpreted solely by sssom-py.
    """
    body = False
    header = []
    for line in StringIO(text):
        operation.consume("work", len(line) + 1)
        if not body and line.startswith("#"):
            operation.consume("states")
            operation.consume("bytes", 4 * len(line))
            header.append(line.lstrip("#").rstrip())
            continue
        body = True
        if "#" in line:
            raise _Unsupported("The supported TSV body cannot contain '#' characters")
    return "\n".join(header)


def _metadata_keys(header: str, operation: Operation) -> frozenset[str]:
    """Qualify maintained YAML nodes for loss hazards, without reading fields."""
    import yaml  # type: ignore[import-untyped]

    root = yaml.compose(header, Loader=yaml.SafeLoader)
    if root is None:
        return frozenset()
    if not isinstance(root, yaml.MappingNode):
        raise _Unsupported("SSSOM metadata requires a simple mapping")
    pending = [(root, 0, False)]
    active: set[int] = set()
    complete: set[int] = set()
    while pending:
        node, depth, leaving = pending.pop()
        operation.consume("work")
        if leaving:
            active.remove(id(node))
            complete.add(id(node))
            continue
        if depth > operation.limits.max_depth:
            raise LimitExceeded("SSSOM metadata exceeds max_depth")
        if id(node) in active:
            raise _Unsupported(
                "Cyclic YAML aliases are outside the supported metadata profile"
            )
        if id(node) in complete:
            continue
        operation.consume("states")
        if isinstance(node, yaml.ScalarNode):
            bound_text(node.value, operation)
            continue
        active.add(id(node))
        pending.append((node, depth, True))
        if isinstance(node, yaml.MappingNode):
            names: set[str] = set()
            operation.consume("states", 2 * len(node.value))
            for key, value in node.value:
                if (
                    not isinstance(key, yaml.ScalarNode)
                    or key.tag != "tag:yaml.org,2002:str"
                ):
                    raise _Unsupported(
                        "YAML merges and complex metadata keys are unsupported"
                    )
                bound_text(key.value, operation)
                if key.value in names:
                    raise InputError("Duplicate SSSOM metadata keys are not permitted")
                names.add(key.value)
                pending.append((value, depth + 1, False))
        elif isinstance(node, yaml.SequenceNode):
            operation.consume("states", len(node.value))
            pending.extend((value, depth + 1, False) for value in node.value)
        else:
            raise _Unsupported("Unsupported YAML metadata node")
    operation.consume("work", len(root.value))
    operation.consume("states", len(root.value))
    return frozenset(key.value for key, _value in root.value)


def _capture(frame: Any, operation: Operation) -> tuple[list[str], list[list[str]]]:
    """Own lexical cells before retention; preserve every original occurrence."""
    count, width = len(frame.index), len(frame.columns)
    operation.consume("states", count * (width + 1) + width)
    columns = []
    for name in frame.columns:
        if not isinstance(name, str):
            raise InputError("Mapping table columns must be textual")
        bound_text(name, operation)
        columns.append(name)
    if not columns or len(set(columns)) != len(columns):
        raise InputError("Mapping table columns must be nonempty and unique")
    rows = []
    for row in frame.itertuples(index=False, name=None):
        values = []
        for value in row:
            if not isinstance(value, str):
                raise _Unsupported(
                    "The maintained raw table did not retain textual cells"
                )
            bound_text(value, operation)
            values.append(value)
        rows.append(values)
    return columns, rows


def _metadata(value: object, operation: Operation) -> str:
    """Use the existing bounded JSON ownership primitive, not a metadata parser."""
    owned = own_json(
        value,
        max_bytes=operation.limits.max_bytes,
        max_depth=operation.limits.max_depth,
    )
    text = json.dumps(
        owned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    bound_text(text, operation)
    return text


def _prepare(request: dict[str, Any], operation: Operation) -> dict[str, object]:
    """Qualify raw occurrences and actual schema semantics using public APIs."""
    try:
        import pandas as pd  # type: ignore[import-untyped]
        from sssom.constants import SSSOMSchemaView
        from sssom.parsers import parse_sssom_table
        from sssom.util import MappingSetDataFrame
        from sssom.validators import validate
        from sssom.writers import write_table
        from sssom_schema.datamodel.sssom_schema import (  # type: ignore[import-untyped]
            EntityTypeEnum,
        )
    except ImportError as error:
        raise _Unsupported(
            "SSSOM interchange requires bijux-ontology[sssom]"
        ) from error
    if version("sssom") != "0.4.21" or version("sssom-schema") != "1.1.0a5":
        raise _Unsupported(
            "SSSOM backend/schema versions are outside the qualified profile"
        )
    encoded = request.get("source")
    if not isinstance(encoded, str):
        raise InputError("SSSOM protocol requires encoded owned bytes")
    bound_text(encoded, operation)
    content = base64.b64decode(encoded, validate=True)
    if len(content) > operation.limits.max_bytes:
        raise LimitExceeded("SSSOM source exceeds max_bytes")
    operation.consume("bytes", len(content))
    text = content.decode("utf-8")
    header = _framing(text, operation)
    authored_keys = _metadata_keys(header, operation)
    supplied = {}
    for key in ("mapping_set_id", "license"):
        value = request.get(key)
        if not isinstance(value, str):
            raise InputError("Explicit mapping set identity and license are required")
        bound_text(value, operation)
        absolute_iri(value)
        supplied[key] = value
    handler = _Warnings()
    logger = logging.getLogger()
    logger.addHandler(handler)
    try:
        with warnings.catch_warnings(record=True) as emitted:
            # The released strict parser inverts its metadata validity flag.
            # Public schema validation plus rejection of every warning qualifies
            # the supported subset without private parser calls or prose rules.
            parsed = parse_sssom_table(
                StringIO(text),
                sep="\t",
                propagate=False,
                strict=False,
                meta=dict(supplied),
            )
            if authored_keys - {"curie_map"} - parsed.metadata.keys():
                raise _Unsupported("Maintained SSSOM parsing omitted authored metadata")
            if any(
                parsed.metadata.get(key) != value for key, value in supplied.items()
            ):
                raise InputError(
                    "Declared mapping identity/license differs from supplied metadata"
                )
            raw = pd.read_csv(
                StringIO(text),
                sep="\t",
                comment="#",
                dtype=str,
                keep_default_na=False,
                engine="python",
                index_col=False,
                on_bad_lines="error",
            )
            columns, rows = _capture(raw, operation)
            if not rows:
                raise InputError("A required mapping set contains no row occurrences")
            maintained = MappingSetDataFrame.with_converter(
                df=raw, converter=parsed.converter, metadata=parsed.metadata
            )
            reports = validate(maintained, fail_on_error=False)
            if any(report.results for report in reports.values()):
                raise InputError("Maintained SSSOM schema validation failed")
            metadata = _metadata(maintained.metadata, operation)
            prefixes = []
            for prefix, iri in maintained.prefix_map.items():
                bound_text(prefix, operation)
                bound_text(iri, operation)
                absolute_iri(iri)
                operation.consume("states")
                prefixes.append([prefix, iri])
            mandatory = (
                "subject_id",
                "predicate_id",
                "object_id",
                "mapping_justification",
            )
            if any(key not in columns for key in mandatory):
                raise InputError(
                    "Mapping rows lack mandatory identifier/justification fields"
                )
            indexes = [columns.index(key) for key in mandatory]
            expanded = []
            row_types = []
            schema = SSSOMSchemaView()
            known_slots = frozenset(schema.mapping_slots)
            uninterpreted = [name for name in columns if name not in known_slots]
            for row in rows:
                cells = dict(zip(columns, row, strict=True))
                meanings = []
                for name in ("subject_type", "object_type", "predicate_type"):
                    declared = cells.get(name) or maintained.metadata.get(name, "")
                    if not isinstance(declared, str):
                        raise _Unsupported(
                            "Endpoint type metadata is not a supported scalar"
                        )
                    meaning = str(EntityTypeEnum(declared).meaning) if declared else ""
                    if meaning:
                        bound_text(meaning, operation)
                        absolute_iri(meaning)
                    meanings.append(meaning)
                row_types.append(meanings)
                result = []
                for index in indexes:
                    value = maintained.converter.expand(row[index])
                    if value is None:
                        raise InputError(
                            "Mapping CURIE lacks an explicit or built-in prefix binding"
                        )
                    bound_text(value, operation)
                    absolute_iri(value)
                    result.append(value)
                expanded.append(result)
            operation.consume("work")
            # All output cells/metadata are bounded before maintained formatting.
            # Reserve its conservative escaped text before constructing a buffer.
            estimate = (
                12
                * (
                    len(metadata)
                    + sum(len(cell) for row in rows for cell in row)
                    + sum(len(c) for c in columns)
                )
                + 4096
            )
            operation.consume("bytes", estimate)
            output = StringIO()
            write_table(
                maintained, output, embedded_mode=True, sort=False, condense=False
            )
            written = output.getvalue()
            bound_text(written, operation)
            reread = pd.read_csv(
                StringIO(written),
                sep="\t",
                comment="#",
                dtype=str,
                keep_default_na=False,
                engine="python",
                index_col=False,
                on_bad_lines="error",
            )
            if _capture(reread, operation) != (columns, rows):
                raise _Unsupported(
                    "Maintained TSV output changed lexical row occurrences"
                )
            again = parse_sssom_table(
                StringIO(written),
                sep="\t",
                propagate=False,
                strict=False,
                meta=dict(supplied),
            )
            if _metadata(again.metadata, operation) != metadata or dict(
                again.prefix_map
            ) != dict(prefixes):
                raise _Unsupported(
                    "Maintained TSV output changed mapping metadata or prefixes"
                )
            if handler.seen or emitted:
                raise _Unsupported(
                    "Maintained SSSOM processing reported an unsupported warning state"
                )
            return {
                "columns": columns,
                "rows": rows,
                "expanded": expanded,
                "row_types": row_types,
                "uninterpreted": uninterpreted,
                "metadata": metadata,
                "prefixes": prefixes,
                "content": base64.b64encode(written.encode()).decode("ascii"),
                "backend": version("sssom"),
                "schema": version("sssom-schema"),
            }
    finally:
        logger.removeHandler(handler)


def prepare(payload: bytes, cap: int) -> bytes:
    """Own the fixed domain protocol; the shared transport owns process lifetime."""
    request = object_value(
        decode_json(payload, max_bytes=cap, max_depth=256), location="mapping"
    )
    limits = Limits(
        **cast(dict[str, int], object_value(request.get("limits"), location="mapping"))
    )
    timeout = request.get("timeout")
    if type(timeout) not in (int, float):
        raise InputError("Mapping worker requires a finite timeout")
    operation = Operation(limits, None, cast(float, timeout), None)
    socket.create_connection = _offline
    socket.socket.connect = _offline  # type: ignore[method-assign]
    try:
        result = _prepare(request, operation)
        operation.consume("bytes", json_bound(result, operation))
    except (LimitExceeded, RecursionError):
        result = {
            "error": "exhausted",
            "message": "SSSOM processing exhausted its configured bounds",
        }
    except _Unsupported as error:
        result = {"error": "unverified", "message": str(error)}
    except (InputError, ValueError, TypeError, UnicodeError, KeyError):
        # Processor exception strings can contain the complete offending source.
        result = {
            "error": "input_error",
            "message": "Maintained SSSOM input could not be qualified",
        }
    encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > cap:
        raise LimitExceeded("SSSOM output exceeds protocol capacity")
    return encoded


if __name__ == "__main__":
    capacity = int(sys.argv[1])
    data = sys.stdin.buffer.read(capacity + 1)
    if len(data) > capacity:
        raise LimitExceeded("SSSOM input exceeds protocol capacity")
    sys.stdout.buffer.write(prepare(data, capacity))
