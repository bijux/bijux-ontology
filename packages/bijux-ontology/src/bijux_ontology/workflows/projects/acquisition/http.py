"""Operation-local HTTP retrieval without ambient credentials or proxies."""

import hashlib
import time
from dataclasses import dataclass, replace
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import bound_text
from bijux_ontology.rdf.compression import decode_gzip

from .types import AcquiredResource, AcquisitionPolicy, _http_url


@dataclass(frozen=True)
class _Response:
    """One owned decoded representation and bounded original header values."""

    resource: AcquiredResource
    payload: bytes
    link: str
    content_type: str = ""
    headers: tuple[tuple[str, str], ...] = ()


class _Redirects(HTTPRedirectHandler):
    """Authorize every new redirect target before urllib opens its connection."""

    def __init__(self, owner: "HttpAcquisition") -> None:
        self.owner = owner
        self.visited: set[str] = set()
        self.count = 0

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request:
        """Keep redirects within explicit origins and the current request allowance."""
        self.owner.authorize(newurl)
        if newurl in self.visited:
            raise ResourceError("Acquisition redirect loop")
        if self.count >= self.owner.policy.max_redirects:
            raise LimitExceeded("Acquisition exceeds max_redirects")
        self.owner.budget.consume("states")
        self.visited.add(newurl)
        self.count += 1
        self.owner.request()
        return Request(newurl, headers=self.owner.headers, method="GET")


class HttpAcquisition:
    """Own bounded responses within one explicit offline-preparation operation."""

    headers = {
        "Accept": "application/ld+json, text/turtle, application/rdf+xml, "
        "application/trig, application/n-quads, application/n-triples, "
        "application/json",
        "Accept-Encoding": "gzip, identity",
    }

    def __init__(
        self, policy: AcquisitionPolicy, budget: Operation, *, accept: str | None = None
    ) -> None:
        self.policy, self.budget = policy, budget
        self.headers = dict(type(self).headers)
        if accept is not None:
            if not isinstance(accept, str):
                raise InputError("Acquisition Accept requires text")
            bound_text(accept, budget)
            if (
                not accept.strip()
                or not accept.isascii()
                or any(ord(char) < 32 or ord(char) == 127 for char in accept)
            ):
                raise InputError(
                    "Acquisition Accept requires printable ASCII header text"
                )
            self.headers["Accept"] = str(accept)
        for origin in policy.origins:
            bound_text(origin, budget)
            budget.consume("states")
        self.origins = frozenset(policy.origins)
        self.responses: dict[str, _Response] = {}
        self.requests = 0
        self.redirects = _Redirects(self)
        self.opener = build_opener(ProxyHandler({}), self.redirects)
        self.opener.addheaders = []

    def authorize(self, url: str) -> str:
        """Validate each requested identity before hashing, lookup or connection."""
        actual, origin = _http_url(url, self.budget)
        if origin not in self.origins:
            raise ResourceError(
                "Acquisition URL is outside the allowed origins", location=actual
            )
        return actual

    def request(self) -> None:
        """Count all network requests, including redirect hops, cumulatively."""
        self.budget.consume("work")
        if self.requests >= self.policy.max_requests:
            raise LimitExceeded("Acquisition exceeds max_requests")
        self.budget.consume("resources")
        self.requests += 1

    def get(self, url: str, *, fresh: bool = False) -> _Response:
        """Read bounded bytes; fresh observations bypass cache lookup and storage.

        Fresh reads still spend this operation's original request and resource
        allowances. Their changing representations do not replace or conflict
        with a previously cached ordinary closure resource.
        """
        if type(fresh) is not bool:
            raise InputError("Acquisition fresh selection requires a boolean")
        url = self.authorize(url)
        if not fresh and url in self.responses:
            return self.responses[url]
        self.request()
        self.redirects.visited = {url}
        self.redirects.count = 0
        remaining = self.budget.deadline - time.monotonic()
        if remaining <= 0:
            raise LimitExceeded("Acquisition exceeded its deadline")
        try:
            with self.opener.open(
                Request(url, headers=self.headers, method="GET"), timeout=remaining
            ) as response:
                effective = self.authorize(response.geturl())
                for key, value in response.headers.items():
                    bound_text(key, self.budget)
                    bound_text(value, self.budget)
                for name in (
                    "Content-Type",
                    "Content-Encoding",
                    "Content-Length",
                    "Transfer-Encoding",
                ):
                    if len(response.headers.get_all(name, [])) > 1:
                        raise InputError("Ambiguous acquisition response headers")
                self.budget.consume("bytes", 64 * len(response.headers))
                headers = tuple(response.headers.items())
                charset = response.headers.get_content_charset()
                if charset is not None and charset.lower() not in ("utf-8", "utf8"):
                    raise InputError("Acquisition supports explicit UTF-8 media only")
                content_type = response.headers.get("Content-Type", "")
                bound_text(content_type, self.budget)
                media_type = response.headers.get_content_type().lower()
                encoding = (
                    response.headers.get("Content-Encoding", "identity").strip().lower()
                )
                if encoding not in ("identity", "gzip"):
                    raise InputError("Unsupported acquisition Content-Encoding")
                link = ",".join(response.headers.get_all("Link", []))
                bound_text(link, self.budget)
                cap = min(
                    self.policy.max_compressed_bytes, self.budget.limits.max_bytes
                )
                length_text = response.headers.get("Content-Length")
                transfer = response.headers.get("Transfer-Encoding")
                if transfer is not None and (
                    transfer.lower() != "chunked" or length_text is not None
                ):
                    raise InputError("Unsupported or ambiguous HTTP body framing")
                expected_size = None
                if length_text is not None:
                    if (
                        not length_text.isascii()
                        or not length_text.isdigit()
                        or len(length_text) > 20
                    ):
                        raise InputError("Invalid HTTP Content-Length")
                    expected_size = int(length_text)
                    if expected_size > cap:
                        raise LimitExceeded(
                            "Acquisition Content-Length exceeds wire cap"
                        )
                payload = bytearray()
                while True:
                    self.budget.consume("work")
                    chunk = response.read(min(65_536, cap + 1 - len(payload)))
                    if not chunk:
                        break
                    if len(payload) + len(chunk) > cap:
                        raise LimitExceeded("Acquisition exceeds compressed byte bound")
                    self.budget.consume("bytes", len(chunk))
                    payload.extend(chunk)
                if expected_size is not None and len(payload) != expected_size:
                    raise ResourceError("Truncated acquisition HTTP body", location=url)
        except HTTPError as error:
            error.close()
            raise ResourceError(
                "Acquisition HTTP response failed", location=url
            ) from error
        except (URLError, OSError, ValueError, HTTPException) as error:
            if isinstance(error, InputError):
                raise
            raise ResourceError(
                "Acquisition HTTP retrieval failed", location=url
            ) from error
        raw = bytes(payload)
        expanded = (
            decode_gzip(raw, max_bytes=self.budget.limits.max_bytes, budget=self.budget)
            if encoding == "gzip"
            else raw
        )
        self.budget.consume("work", len(expanded) + 1)
        self.budget.consume("states")
        resource = AcquiredResource(
            url,
            effective,
            media_type,
            None,
            encoding,
            len(raw),
            len(expanded),
            hashlib.sha256(expanded).hexdigest(),
        )
        result = _Response(resource, expanded, link, content_type, headers)
        if fresh:
            return result
        before = self.responses.get(effective)
        if before is not None:
            self.budget.consume("work", len(expanded) + 1)
            if before.payload != expanded or before.resource.media_type != media_type:
                raise ResourceError(
                    "Conflicting acquired effective URL", location=effective
                )
        self.responses[url] = result
        if effective not in self.responses:
            self.budget.consume("states")
            self.responses[effective] = replace(
                result, resource=replace(resource, requested_url=effective)
            )
        return result
