"""Owned preparation process; HTTP and maintained parsers share one deadline."""

import base64
import json
import sys
from dataclasses import asdict
from io import BytesIO
from typing import cast
from zipfile import Path as ZipPath
from zipfile import ZipFile

from bijux_ontology._json import decode_json, object_value
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Limits, bound_text
from bijux_ontology.workflows.projects.bundles import load_bundle

from .closure import Closure
from .http import HttpAcquisition
from .types import AcquisitionPolicy, _Unavailable


def _failure(error: InputError, operation: Operation) -> dict[str, object]:
    """Keep bounded actual error metadata; never fabricate parser positions."""
    kind = (
        "exhausted"
        if isinstance(error, LimitExceeded)
        else "unverified"
        if isinstance(error, _Unavailable)
        else "input_error"
    )
    message, location = str(error), error.location
    failure = error.failure
    try:
        bound_text(message, operation)
        bound_text(location, operation)
        if failure is not None:
            for value in (
                failure.document_iri,
                failure.location,
                failure.role,
                *failure.import_chain,
            ):
                if value is not None:
                    bound_text(value, operation)
    except LimitExceeded:
        return {
            "kind": "exhausted",
            "message": "Acquisition exhausted its metadata allowance",
            "location": "",
            "failure": None,
        }
    return {
        "kind": kind,
        "message": message,
        "location": location,
        "failure": asdict(failure) if failure is not None else None,
    }


def prepare(payload: bytes, cap: int) -> bytes:
    """Consume a bounded domain request and return only owned archive/evidence."""
    request = object_value(
        decode_json(payload, max_bytes=cap, max_depth=256), location="acquisition"
    )
    limits = Limits(
        **cast(
            dict[str, int], object_value(request.get("limits"), location="acquisition")
        )
    )
    timeout = request.get("timeout")
    if type(timeout) not in (int, float):
        raise InputError("Acquisition worker requires a finite timeout")
    operation = Operation(limits, None, cast(float, timeout), None)
    raw_policy = object_value(request.get("policy"), location="acquisition")
    policy = AcquisitionPolicy(
        tuple(cast(list[str], raw_policy.get("origins", ()))),
        cast(int, raw_policy.get("max_redirects")),
        cast(int, raw_policy.get("max_requests")),
        cast(int, raw_policy.get("max_compressed_bytes")),
    )
    http = None
    archive = None
    error_data = None
    try:
        http = HttpAcquisition(policy, operation)
        operation.consume("bytes", len(payload))
        raw_files = object_value(request.get("files"), location="acquisition")
        files = {}
        for name, value in raw_files.items():
            bound_text(name, operation)
            if not isinstance(value, str):
                raise InputError("Acquisition files require base64 strings")
            operation.consume("work", len(value))
            operation.consume("bytes", 3 * ((len(value) + 3) // 4))
            files[name] = base64.b64decode(value, validate=True)
            operation.consume("resources")
        manifest = object_value(request.get("manifest"), location="acquisition")
        requests = object_value(request.get("requests"), location="acquisition")
        for path, url in requests.items():
            bound_text(path, operation)
            if not isinstance(url, str):
                raise InputError("Acquisition request URLs require strings")
            bound_text(url, operation)
        closure = Closure(manifest, files, cast(dict[str, str], requests), http)
        candidate = closure.prepare()
        with ZipFile(BytesIO(candidate)) as zipped:
            load_bundle(ZipPath(zipped), limits=operation.limits)
        operation.consume("work")
        archive = candidate
    except LimitExceeded as error:
        error_data = _failure(error, operation)
    except InputError as error:
        error_data = _failure(error, operation)
    records = []
    for url, response in http.responses.items() if http is not None else ():
        # A cached effective alias remains a distinct reviewable logical binding.
        if response.resource.requested_url != url:
            continue
        records.append(asdict(response.resource))
    encoded_archive = None
    if archive is not None:
        allowance = 4 * ((len(archive) + 2) // 3)
        if allowance > cap:
            raise LimitExceeded("Acquired archive exceeds protocol cap")
        encoded_archive = base64.b64encode(archive).decode("ascii")
    result = {"archive": encoded_archive, "resources": records, "error": error_data}
    # Reserve ASCII escapes for bounded metadata before encoding the response.
    size = sum(
        12 * len(value) + 2
        for record in records
        for value in record.values()
        if isinstance(value, str)
    )
    size += len(encoded_archive) if encoded_archive is not None else 0
    if error_data is not None:
        size += 12 * len(str(error_data))
    if size + 4096 > cap:
        raise LimitExceeded("Acquisition response exceeds protocol cap")
    result_bytes = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
    if len(result_bytes) > cap:
        raise LimitExceeded("Acquisition response exceeds protocol cap")
    return result_bytes


def main() -> None:
    """Enter only through the explicitly authorized owned-worker transport."""
    cap = int(sys.argv[1])
    payload = sys.stdin.buffer.read(cap + 1)
    if len(payload) > cap:
        raise LimitExceeded("Acquisition request exceeds protocol cap")
    sys.stdout.buffer.write(prepare(payload, cap))


if __name__ == "__main__":
    main()
