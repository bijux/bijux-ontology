"""Maintained markup processors inline in one bounded owned native worker."""

import json
import sys
from typing import Any

from bijux_ontology._json import decode_json
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.patches.correspondence import _ControlBudget, _json_bound
from bijux_ontology.rdf.terms import bound_text

_START = b"<bijux-fragment>"
_END = b"</bijux-fragment>"


def _parse(text: str, kind: str, budget: Budget) -> Any:
    """Use native fragment parsing with all external resolution disabled."""
    from lxml import etree, html  # type: ignore[import-untyped]

    class Deny(etree.Resolver):  # type: ignore[misc]
        """Refuse local resources as well as network resolutions."""

        def resolve(self, url: str, public: str, context: Any) -> Any:
            """Never delegate an external lookup to native default resolvers."""
            raise InputError("Markup resource resolution is unavailable")

    # This conservative lexical exclusion is not a DTD parser. Even a marker
    # inside a comment is outside this finite no-declaration profile.
    budget.consume("work", len(text) + 1)
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise InputError("Markup declarations are outside the offline profile")
    if kind == "xml":
        parser = etree.XMLParser(
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            attribute_defaults=False,
            dtd_validation=False,
            recover=False,
            huge_tree=False,
            remove_comments=False,
            remove_pis=False,
            strip_cdata=True,
            collect_ids=False,
        )
        parser.resolvers.add(Deny())
        budget.consume("bytes", 8 * len(text) + len(_START) + len(_END))
        encoded = text.encode("utf-8")
        payload = b"".join((_START, encoded, _END))
        try:
            root = etree.fromstring(payload, parser=parser)
        except etree.XMLSyntaxError:
            if any(e.type_name == "ERR_RESOURCE_LIMIT" for e in parser.error_log):
                raise LimitExceeded(
                    "Native XML parser resource bound exceeded"
                ) from None
            raise
    else:
        parser = html.HTMLParser(
            no_network=True, huge_tree=False, remove_comments=False, remove_pis=False
        )
        parser.resolvers.add(Deny())
        root = (
            html.fragment_fromstring(
                text, create_parent="bijux-fragment", parser=parser
            )
            if text
            else html.Element("bijux-fragment")
        )
    for error in parser.error_log:
        budget.consume("work")
        if error.type_name == "ERR_RESOURCE_LIMIT" or error.level_name == "FATAL":
            raise LimitExceeded("Native markup parser could not retain complete input")
    return root


def _inspect(root: Any, budget: Budget) -> dict[str, object]:
    """Capture bounded actual parser observations, not a second markup tree."""
    from lxml import etree

    names: list[str] = []
    parts: list[str] = []
    comments = 0
    for node in root.iter():
        budget.consume("work")
        budget.consume("states")
        for depth, _ in enumerate(node.iterancestors(), 1):
            budget.consume("work")
            if depth > budget.limits.max_depth:
                raise LimitExceeded("Markup fragment exceeds max_depth")
        if isinstance(node, etree._Entity):
            raise InputError("Unresolved markup entities have no inspected value")
        if isinstance(node, etree._Comment):
            comments += 1
        if isinstance(node.tag, str):
            if node is not root:
                bound_text(node.tag, budget)
                budget.consume("bytes", 64)
                names.append(node.tag)
            for key, value in node.attrib.items():
                budget.consume("work")
                budget.consume("states")
                bound_text(key, budget)
                bound_text(value, budget)
    for part in root.itertext():
        bound_text(part, budget)
        budget.consume("states")
        parts.append(part)
    size = sum(len(part.encode("utf-8")) for part in parts)
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Inspected markup text exceeds max_bytes")
    budget.consume("bytes", size)
    return {"names": names, "text": "".join(parts), "comments": comments}


def _canonical(root: Any, budget: Budget) -> str:
    """Stream actual exclusive C14N output into a bounded byte sink."""
    from lxml import etree

    class Sink:
        """Bound each native output chunk before retaining or joining it."""

        def __init__(self) -> None:
            """Own only successfully charged output bytes."""
            self.parts: list[bytes] = []
            self.size = 0

        def write(self, chunk: bytes) -> None:
            """Refuse expansion before any unbounded Python output accumulation."""
            if self.size + len(chunk) > budget.limits.max_bytes:
                raise LimitExceeded("XML canonical output exceeds max_bytes")
            budget.consume("bytes", len(chunk))
            budget.consume("work", len(chunk) + 1)
            budget.consume("states")
            self.parts.append(chunk)
            self.size += len(chunk)

    sink = Sink()
    etree.ElementTree(root).write(
        sink, method="c14n", exclusive=True, with_comments=True
    )
    budget.consume("bytes", sink.size * 3)
    encoded = b"".join(sink.parts)
    if not encoded.startswith(_START) or not encoded.endswith(_END):
        raise InputError("Native canonical fragment envelope changed")
    return encoded[len(_START) : -len(_END)].decode("utf-8")


def evaluate(request: dict[str, Any]) -> dict[str, object]:
    """Return only complete native observations and cumulative owned counters."""
    try:
        from lxml import etree
    except ImportError:
        return {"status": "unavailable"}
    budget = _ControlBudget(Limits(**request["limits"]), request["allowances"])
    backend = "lxml " + ".".join(map(str, etree.LXML_VERSION))
    backend += "; libxml2 " + ".".join(map(str, etree.LIBXML_VERSION))
    try:
        text, kind, canonicalization = (
            request["text"],
            request["kind"],
            request["canonicalization"],
        )
        bound_text(text, budget)
        if kind not in ("xml", "html") or canonicalization not in (
            None,
            "xml-exc-c14n-with-comments",
        ):
            raise InputError("Unsupported markup processor request")
        if kind == "html" and canonicalization is not None:
            raise InputError("HTML has no supported XML canonical value")
        budget.consume("resources")
        root = _parse(text, kind, budget)
        result = _inspect(root, budget)
        result.update(
            status="result",
            canonical=_canonical(root, budget) if canonicalization else None,
        )
        budget.consume("bytes", _json_bound(result, budget))
        budget.consume(
            "bytes", _json_bound({"consumed": request["allowances"]}, budget)
        )
    except (LimitExceeded, MemoryError, RecursionError):
        result = {"status": "exhausted"}
    except etree.XMLSyntaxError:
        result = {"status": "input_error"}
    except (etree.LxmlError, InputError, ValueError, TypeError):
        result = {"status": "unverified"}
    result["backend"] = backend
    result["consumed"] = budget.consumed()
    return result


def main() -> int:
    """Read one bounded request and execute no nested process or loader."""
    try:
        cap = int(sys.argv[1])
        raw = sys.stdin.buffer.read(cap + 1)
        request = decode_json(raw, max_bytes=cap)
        if not isinstance(request, dict):
            raise InputError("Expected a markup processor request")
        result = evaluate(request)
        output = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        if len(output) > min(cap, request["output_cap"]):
            output = b'{"status":"exhausted"}'
        sys.stdout.buffer.write(output)
    except (InputError, LimitExceeded, ValueError, MemoryError, RecursionError):
        sys.stdout.buffer.write(b'{"status":"exhausted"}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
