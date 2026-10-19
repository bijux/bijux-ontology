"""Maintained one-hop HTTP I/O with a bounded, parent-owned process lifetime."""

import json
import os
import sys
import threading
import time
from http.client import HTTPException, HTTPMessage, HTTPResponse
from typing import BinaryIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import ControlBudget
from bijux_ontology.execution.limits import BUDGET_KINDS, Limits
from bijux_ontology.json.values import decode_json, object_value

from .http import _interpret_headers
from .http_transport import _FRAME, _MAGIC, _REQUEST, _U32


class _Redirect(Exception):
    """A normalized maintained redirect, before drain or another connection."""

    def __init__(self, target: str, headers: tuple[tuple[str, str], ...]) -> None:
        self.target, self.headers = target, headers


class _Redirects(HTTPRedirectHandler):
    """Stop urllib after it normalizes a target, before its unbounded drain."""

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request:
        cast(BinaryIO, fp).close()
        raise _Redirect(newurl, tuple(headers.items()))


def _owner(parent: int, deadline: int) -> bool:
    """Check the admitted POSIX parent and exact absolute lifetime before I/O."""
    # Windows workers belong to the caller-bound Job supervisor; their direct
    # parent is that supervisor. POSIX workers directly check the admitted PID.
    return time.monotonic_ns() < deadline and (
        os.name != "posix" or os.getppid() == parent
    )


def _watch(done: threading.Event, parent: int, deadline: int) -> None:
    """Bound this process even when an outer preparation worker is terminated."""
    while not done.wait(0.01):
        if not _owner(parent, deadline):
            # Process exit releases owned sockets, including a blocked resolver
            # or TLS/header/body read. This thread never performs network I/O.
            os._exit(124)


def _metadata(
    effective: str,
    target: str,
    message: str,
    headers: tuple[tuple[str, str], ...],
    cap: int,
    budget: ControlBudget,
) -> bytes:
    """Encode only funded UTF-8 fields; the body remains original raw bytes."""
    fields: list[bytes] = []
    size = 16
    for text in (
        effective,
        target,
        message,
        *(part for row in headers for part in row),
    ):
        if len(text) > cap:
            raise LimitExceeded("HTTP hop text exceeds funded metadata")
        budget.consume("work", len(text) + 1)
        budget.consume("bytes", 4 * len(text))
        budget.consume("states")
        encoded = text.encode("utf-8")
        size += len(encoded) + 4
        if size > cap:
            raise LimitExceeded("HTTP hop metadata exceeds funded capacity")
        fields.append(encoded)
    budget.consume("bytes", size)
    budget.consume("states", len(fields))
    first = fields[:3]
    return b"".join(
        (
            *(_U32.pack(len(value)) + value for value in first),
            _U32.pack(len(headers)),
            *(_U32.pack(len(value)) + value for value in fields[3:]),
        )
    )


def _run(payload: bytes, size: int, parent: int, deadline: int) -> None:
    """Interpret only host-authored bounded input with prepaid parser workspace."""
    if len(payload) != size or not _owner(parent, deadline):
        raise LimitExceeded("HTTP hop owner expired before input admission")
    # The host prepays this exact request's maintained parser workspace before
    # dispatch. No caller document or unbounded response enters this bootstrap.
    bootstrap_limits = Limits(
        max_bytes=size,
        max_total_bytes=128 * size + 4096,
        max_work=8 * size + 256,
        max_states=256,
        max_depth=16,
    )
    bootstrap_grants = dict.fromkeys(BUDGET_KINDS, 0)
    bootstrap_grants.update(
        {
            "bytes": bootstrap_limits.max_total_bytes,
            "work": bootstrap_limits.max_work,
            "states": bootstrap_limits.max_states,
        }
    )
    bootstrap = ControlBudget(bootstrap_limits, bootstrap_grants)
    data = object_value(
        decode_json(payload, max_bytes=size, max_depth=16, budget=bootstrap),
        location="HTTP hop",
    )
    limits = Limits(
        **cast(dict[str, int], object_value(data.get("limits"), location="HTTP hop"))
    )
    budget = ControlBudget(limits, data.get("grants"))
    budget.consume("bytes", 8192)
    budget.consume("states", 16)
    url, header_cap, body_cap = (
        data.get("url"),
        data.get("header_cap"),
        data.get("body_cap"),
    )
    supplied = object_value(data.get("headers"), location="HTTP hop")
    if (
        not isinstance(url, str)
        or any(not isinstance(value, str) for value in supplied.values())
        or type(header_cap) is not int
        or not 0 < header_cap <= 2**32 - 1
        or type(body_cap) is not int
        or not 0 < body_cap <= 2**63 - 1
    ):
        raise InputError("Invalid bounded HTTP hop request")
    kind, effective, target, message = 0, "", "", ""
    headers: tuple[tuple[str, str], ...] = ()
    body = bytearray()
    try:
        if not _owner(parent, deadline):
            raise LimitExceeded("HTTP hop owner expired before connection")
        opener = build_opener(ProxyHandler({}), _Redirects())
        opener.addheaders = []
        with opener.open(
            Request(url, headers=cast(dict[str, str], supplied), method="GET"),
            timeout=max(0.000001, (deadline - time.monotonic_ns()) / 1e9),
        ) as response:
            response = cast(HTTPResponse, response)
            effective = response.geturl()
            facts = _interpret_headers(response.headers, budget, body_cap)
            budget.consume("states", len(response.headers))
            headers = tuple(response.headers.items())
            while True:
                budget.consume("work")
                chunk = response.read(min(65536, body_cap + 1 - len(body)))
                if not chunk:
                    break
                if len(body) + len(chunk) > body_cap:
                    raise LimitExceeded("Acquisition exceeds compressed byte bound")
                budget.consume("bytes", 2 * len(chunk))
                body.extend(chunk)
            if facts.expected_size is not None and len(body) != facts.expected_size:
                raise ResourceError("Truncated acquisition HTTP body")
    except _Redirect as redirect:
        kind, target, headers = 1, redirect.target, redirect.headers
    except HTTPError as error:
        error.close()
        kind, message = 4, "Acquisition HTTP response failed"
    except LimitExceeded as error:
        kind, message = 3, str(error)
    except InputError as error:
        kind, message = (4 if isinstance(error, ResourceError) else 2), str(error)
    except (URLError, OSError, ValueError, HTTPException):
        kind, message = 4, "Acquisition HTTP retrieval failed"
    if kind in (2, 3, 4):
        effective, target, headers, body = "", "", (), bytearray()
    try:
        metadata = _metadata(effective, target, message, headers, header_cap, budget)
    except LimitExceeded:
        kind, effective, target, headers, body = 3, "", "", (), bytearray()
        message = "HTTP hop exhausted bounded metadata"
        # This constant failure envelope and its encoding are prepaid fixed
        # native storage, not a renewed allowance after capture exhaustion.
        values = (b"", b"", message.encode("ascii"))
        metadata = b"".join(
            _U32.pack(len(value)) + value for value in values
        ) + _U32.pack(0)
    counters = json.dumps(budget.consumed(), separators=(",", ":")).encode("ascii")
    if len(counters) > 1024:
        raise LimitExceeded("HTTP hop counter envelope exceeds fixed capacity")
    out = sys.stdout.buffer
    out.write(_FRAME.pack(_MAGIC, kind, len(counters), len(metadata), len(body)))
    out.write(counters)
    out.write(metadata)
    out.write(memoryview(body))
    out.flush()


def main() -> None:
    """Own the watchdog from admitted fixed identity through the final write."""
    cap = int(sys.argv[1])
    prefix = sys.stdin.buffer.read(_REQUEST.size)
    if len(prefix) != _REQUEST.size:
        raise InputError("Missing HTTP hop lifetime identity")
    size, parent, deadline = _REQUEST.unpack(prefix)
    if not 0 < size <= cap or not _owner(parent, deadline):
        raise LimitExceeded("HTTP hop owner expired before startup")
    done = threading.Event()
    watcher = threading.Thread(
        target=_watch, args=(done, parent, deadline), daemon=True
    )
    watcher.start()
    try:
        payload = sys.stdin.buffer.read(size + 1)
        _run(payload, size, parent, deadline)
    finally:
        done.set()
        watcher.join(0.1)
        if watcher.is_alive():
            raise ResourceError("HTTP lifetime watcher did not retire")


if __name__ == "__main__":
    main()
