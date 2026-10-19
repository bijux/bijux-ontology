"""Explicit retrieval selections and bounded offline acquisition evidence."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from bijux_ontology.documents.archive import resource_path
from bijux_ontology.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Report


def _http_url(value: str, budget: Budget) -> tuple[str, str]:
    """Qualify a credential-free HTTP URL before parsing or retaining metadata."""
    if type(value) is not str:
        raise InputError("Acquisition URLs require plain strings")
    bound_text(value, budget)
    absolute_iri(value)
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise InputError("Acquisition URLs cannot contain control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = parsed.hostname
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("Unsupported retrieval identity")
        host = host.encode("idna").decode("ascii").lower()
    except (ValueError, UnicodeError) as error:
        raise InputError("Acquisition requires credential-free HTTP(S) URLs") from error
    if ":" in host:
        host = "[" + host + "]"
    default = 80 if parsed.scheme == "http" else 443
    origin = parsed.scheme + "://" + host
    if port is not None and port != default:
        origin += ":" + str(port)
    bound_text(origin, budget)
    return value, origin


class _Unavailable(InputError):
    """A required optional maintained processor cannot qualify this closure."""


@dataclass(frozen=True)
class AcquisitionRequest:
    """Retrieve one declared portable project member from an explicit URL."""

    path: str
    url: str

    def __post_init__(self) -> None:
        """Reject nonportable paths and credentials before any retrieval."""
        budget = Budget()
        if type(self.path) is not str:
            raise InputError("Acquisition paths require plain strings")
        bound_text(self.path, budget)
        resource_path(self.path)
        _http_url(self.url, budget)


@dataclass(frozen=True)
class AcquisitionPolicy:
    """Exact authorized origins and per-operation request/representation bounds."""

    origins: tuple[str, ...]
    max_redirects: int = 5
    max_requests: int = 100
    max_compressed_bytes: int = 5_000_000

    def __post_init__(self) -> None:
        """Freeze finite origin choices and positive machine-sized capacities."""
        budget = Budget()
        supplied: Sequence[str] = self.origins
        origins = capture_items(supplied, str, budget)
        normalized = []
        seen = set()
        for value in origins:
            _, origin = _http_url(value, budget)
            parsed = urlsplit(value)
            if parsed.path not in ("", "/") or parsed.query:
                raise InputError("Allowed origins cannot include paths or queries")
            budget.consume("states")
            if origin in seen:
                raise InputError("Acquisition origins must be distinct")
            seen.add(origin)
            normalized.append(origin)
        if not normalized:
            raise InputError("Acquisition requires explicit allowed origins")
        for capacity in (
            self.max_redirects,
            self.max_requests,
            self.max_compressed_bytes,
        ):
            if type(capacity) is not int or capacity < 0 or capacity.bit_length() > 63:
                raise InputError(
                    "Acquisition capacities require positive bounded integers"
                )
        if self.max_requests == 0 or self.max_compressed_bytes == 0:
            raise InputError("Request and byte capacities must be positive")
        object.__setattr__(self, "origins", tuple(normalized))


@dataclass(frozen=True)
class AcquiredResource:
    """Original request/effective identity and exact expanded payload metadata."""

    requested_url: str
    effective_url: str
    media_type: str
    context_url: str | None
    encoding: str
    compressed_bytes: int
    expanded_bytes: int
    sha256: str


@dataclass(frozen=True)
class BundleAcquisition:
    """A qualified ordinary offline archive or a bounded incomplete preparation."""

    archive: Path | None
    resources: tuple[AcquiredResource, ...]
    report: Report
