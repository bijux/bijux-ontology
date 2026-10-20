"""Finite gzip document expansion using an owned standard-library decoder."""

import gzip
import zlib
from io import BytesIO

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.limits import Budget


def decode_gzip(payload: bytes, *, max_bytes: int, budget: Budget) -> bytes:
    """Decode all members and validate trailers before publishing any RDF bytes."""
    if not payload:
        raise InputError("Invalid gzip RDF content: no member supplied")
    expanded = bytearray()
    try:
        with (
            BytesIO(payload) as compressed,
            gzip.GzipFile(fileobj=compressed, mode="rb") as decoder,
        ):
            while True:
                budget.consume("work")
                chunk = decoder.read(min(65_536, max_bytes + 1 - len(expanded)))
                if not chunk:
                    return bytes(expanded)
                if len(expanded) + len(chunk) > max_bytes:
                    raise LimitExceeded("Expanded gzip RDF exceeds max_bytes")
                budget.consume("bytes", len(chunk))
                expanded.extend(chunk)
    except (OSError, EOFError, zlib.error) as error:
        raise InputError("Invalid or truncated gzip RDF content") from error
