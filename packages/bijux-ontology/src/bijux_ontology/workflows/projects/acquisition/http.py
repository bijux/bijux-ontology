"""Operation-local HTTP retrieval without ambient credentials or proxies."""

import hashlib
import re
from dataclasses import dataclass, replace
from email.message import Message
from urllib.parse import urljoin

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.sources.compression import decode_gzip

from .http_transport import read_hop
from .types import AcquiredResource, AcquisitionPolicy, _http_url

_CONTEXT_RELATION = "http://www.w3.org/ns/json-ld#context"
_CONTEXT_ATTRIBUTE = re.compile(
    r'(?:^|;)\s*rel="' + re.escape(_CONTEXT_RELATION) + r'"'
)


@dataclass(frozen=True)
class _Response:
    """One owned decoded representation and bounded original header values."""

    resource: AcquiredResource
    payload: bytes
    link: str
    content_type: str = ""
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _HeaderFacts:
    """One maintained header interpretation used before and after transport."""

    content_type: str
    media_type: str
    encoding: str
    link: str
    expected_size: int | None


def _interpret_headers(headers: Message, budget: Budget, cap: int) -> _HeaderFacts:
    """Keep original media, encoding and body-framing admission in one owner."""
    for key, value in headers.items():
        bound_text(key, budget)
        bound_text(value, budget)
    for name in (
        "Content-Type",
        "Content-Encoding",
        "Content-Length",
        "Transfer-Encoding",
    ):
        if len(headers.get_all(name, [])) > 1:
            raise InputError("Ambiguous acquisition response headers")
    budget.consume("bytes", 64 * len(headers))
    charset = headers.get_content_charset()
    if charset is not None and charset.lower() not in ("utf-8", "utf8"):
        raise InputError("Acquisition supports explicit UTF-8 media only")
    content_type = headers.get("Content-Type", "")
    bound_text(content_type, budget)
    media_type = headers.get_content_type().lower()
    encoding = headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("identity", "gzip"):
        raise InputError("Unsupported acquisition Content-Encoding")
    link = ",".join(headers.get_all("Link", []))
    bound_text(link, budget)
    length_text = headers.get("Content-Length")
    transfer = headers.get("Transfer-Encoding")
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
            raise LimitExceeded("Acquisition Content-Length exceeds wire cap")
    return _HeaderFacts(content_type, media_type, encoding, link, expected_size)


class _Redirects:
    """Authorize each normalized redirect before admitting its next owned hop."""

    def __init__(self, owner: "HttpAcquisition") -> None:
        self.owner = owner
        self.visited: set[str] = set()
        self.count = 0

    def follow(self, newurl: str) -> None:
        """Keep actual redirect targets within the original request allowance."""
        self.owner.authorize(newurl)
        if newurl in self.visited:
            raise ResourceError("Acquisition redirect loop")
        if self.count >= self.owner.policy.max_redirects:
            raise LimitExceeded("Acquisition exceeds max_redirects")
        self.owner.budget.consume("states")
        self.visited.add(newurl)
        self.count += 1
        self.owner.request()


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

    def authorize(self, url: str) -> str:
        """Validate each requested identity before hashing, lookup or connection."""
        actual, origin = _http_url(url, self.budget)
        if origin not in self.origins:
            raise ResourceError(
                "Acquisition URL is outside the allowed origins", location=actual
            )
        return actual

    def _context_url(self, effective: str, media_type: str, link: str) -> str | None:
        """Qualify processing-relevant Link meaning before admitting an alias."""
        if (
            media_type == "application/ld+json"
            or not (
                media_type == "application/json"
                or media_type.startswith("application/")
                and media_type.endswith("+json")
            )
            or _CONTEXT_ATTRIBUTE.search(link) is None
        ):
            return None
        # This candidate gate preserves core retrieval for unrelated Link headers;
        # only the maintained parser selects relations, targets and ambiguity.
        self.budget.consume("work", len(link) + 1)
        self.budget.consume("bytes", 128 * len(link) + 4096)
        self.budget.consume("states", len(link) + 1)
        linked = processor().parse_link_header(link).get(_CONTEXT_RELATION)
        if isinstance(linked, list):
            raise InputError("Multiple HTTP JSON-LD context links are unsupported")
        if linked is None:
            return None
        if not isinstance(linked, dict) or not isinstance(linked.get("target"), str):
            raise InputError("Malformed HTTP JSON-LD context link")
        target = linked["target"]
        self.budget.consume("bytes", 4 * (len(effective) + len(target)) + 64)
        return self.authorize(urljoin(effective, target))

    def request(self) -> None:
        """Count all network requests, including redirect hops, cumulatively."""
        self.budget.consume("work")
        if self.requests >= self.policy.max_requests:
            raise LimitExceeded("Acquisition exceeds max_requests")
        self.budget.consume("resources")
        self.requests += 1

    def get(self, url: str, *, fresh: bool = False) -> _Response:
        """Read bounded bytes; fresh observations bypass cache lookup and storage.

        Connect, response headers, redirects and body share the absolute
        deadline and cancellation of this operation.

        Fresh reads still spend this operation's original request and resource
        allowances. Their changing representations do not replace or conflict
        with a previously cached ordinary closure resource.
        """
        if type(fresh) is not bool:
            raise InputError("Acquisition fresh selection requires a boolean")
        url = self.authorize(url)
        if not fresh and url in self.responses:
            self.budget.consume("work", 0)
            return self.responses[url]
        self.request()
        self.redirects.visited = {url}
        self.redirects.count = 0
        current = url
        cap = min(self.policy.max_compressed_bytes, self.budget.limits.max_bytes)
        while True:
            try:
                hop = read_hop(current, self.headers, cap, self.budget)
            except ResourceError as error:
                self.budget.consume("work", 0)
                raise ResourceError(
                    "Acquisition HTTP retrieval failed", location=url
                ) from error
            # A paid native receipt never renews permission for a redirect or cache.
            self.budget.consume("work", 0)
            if hop.kind == 1:
                self.redirects.follow(hop.target)
                current = hop.target
                continue
            if hop.kind == 2:
                raise InputError(hop.message)
            if hop.kind == 3:
                raise LimitExceeded(hop.message)
            if hop.kind == 4:
                raise ResourceError(hop.message, location=url)
            effective = self.authorize(hop.effective)
            captured = Message()
            for key, value in hop.headers:
                captured[key] = value
            facts = _interpret_headers(captured, self.budget, cap)
            headers = hop.headers
            content_type, media_type, encoding, link = (
                facts.content_type,
                facts.media_type,
                facts.encoding,
                facts.link,
            )
            raw = hop.body
            if facts.expected_size is not None and len(raw) != facts.expected_size:
                raise ResourceError("Truncated acquisition HTTP body", location=url)
            break
        expanded = (
            decode_gzip(raw, max_bytes=self.budget.limits.max_bytes, budget=self.budget)
            if encoding == "gzip"
            else raw
        )
        self.budget.consume("work", len(expanded) + 1)
        self.budget.consume("states")
        context_url = self._context_url(effective, media_type, link)
        resource = AcquiredResource(
            url,
            effective,
            media_type,
            context_url,
            encoding,
            len(raw),
            len(expanded),
            hashlib.sha256(expanded).hexdigest(),
        )
        result = _Response(resource, expanded, link, content_type, headers)
        self.budget.consume("work", 0)
        if fresh:
            return result
        before = self.responses.get(effective)
        if before is not None:
            self.budget.consume("work", len(expanded) + 1)
            if (
                before.payload != expanded
                or before.resource.media_type != media_type
                or before.resource.context_url != context_url
            ):
                raise ResourceError(
                    "Conflicting acquired effective URL", location=effective
                )
        alias = None
        if effective != url and effective not in self.responses:
            self.budget.consume("states")
            alias = replace(result, resource=replace(resource, requested_url=effective))
        self.budget.consume("work", 0)
        try:
            self.responses[url] = result
            if alias is not None:
                self.responses[effective] = alias
            self.budget.consume("work", 0)
        except LimitExceeded:
            if self.responses.get(url) is result:
                del self.responses[url]
            if alias is not None and self.responses.get(effective) is alias:
                del self.responses[effective]
            raise
        return result
