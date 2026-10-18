"""Validate bounded XML with Expat before the maintained native RDF parser."""

from xml.parsers import expat

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget

from .types import XMLCapabilityError, XMLEncoding, XMLInput


class _Boundary:
    """One fresh non-resolving XML parser and cumulative operation counters."""

    def __init__(self, payload: bytes, budget: Budget, max_bytes: int) -> None:
        """Retain original immutable bytes and establish only BOM encoding facts."""
        self.payload = payload
        self.budget = budget
        self.max_bytes = max_bytes
        self.encoding: XMLEncoding = (
            "utf-16-le"
            if payload.startswith(b"\xff\xfe")
            else "utf-16-be"
            if payload.startswith(b"\xfe\xff")
            else "utf-8"
        )
        if self.encoding == "utf-8" and payload.startswith((b"\x00<", b"<\x00")):
            raise XMLCapabilityError("UTF-16 without a byte-order mark")
        self.parser = expat.ParserCreate(namespace_separator=" ")
        self.parser.buffer_text = False
        if not self.parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER):
            raise XMLCapabilityError("parameter-entity isolation")
        self.depth = 0
        self.in_dtd = False
        self.entities = 0
        self.expanded = 0
        self.declaration_encoding: str | None = None
        self.declaration_seen = False
        self.prefix_end: int | None = None
        self.parser.XmlDeclHandler = self.declaration
        self.parser.StartElementHandler = self.start
        self.parser.StartNamespaceDeclHandler = self.namespace
        self.parser.EndNamespaceDeclHandler = self.end_namespace
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.data
        self.parser.CommentHandler = self.data
        self.parser.ProcessingInstructionHandler = self.instruction
        self.parser.StartDoctypeDeclHandler = self.doctype
        self.parser.EndDoctypeDeclHandler = self.end_doctype
        self.parser.EntityDeclHandler = self.entity
        self.parser.DefaultHandlerExpand = self.default
        self.parser.ElementDeclHandler = self.unsupported_dtd
        self.parser.AttlistDeclHandler = self.unsupported_dtd
        self.parser.NotationDeclHandler = self.unsupported_dtd
        self.parser.UnparsedEntityDeclHandler = self.unsupported_dtd
        self.parser.SkippedEntityHandler = self.skipped
        self.parser.ExternalEntityRefHandler = self.external

    def event(self) -> None:
        """Bound callbacks and the validated prefix after an encoding declaration."""
        self.budget.consume("work")
        if self.declaration_seen and self.prefix_end is None:
            self.prefix_end = self.parser.CurrentByteIndex

    def text(self, value: str, *, expanded: bool = True) -> None:
        """Charge actual UTF-8 callback data before retaining or interpreting it."""
        if len(value) > self.max_bytes:
            raise LimitExceeded("XML content exceeds max_bytes")
        self.budget.consume("work", len(value) + 1)
        size = len(value.encode("utf-8"))
        if size > self.max_bytes:
            raise LimitExceeded("XML content exceeds max_bytes")
        if expanded:
            self.expanded += size
            if self.expanded > self.max_bytes:
                raise LimitExceeded("Expanded XML content exceeds max_bytes")
        self.budget.consume("bytes", size)

    def declaration(self, version: str, encoding: str | None, standalone: int) -> None:
        """Accept the documented encoding set only after actual XML decoding."""
        self.budget.consume("work")
        self.text(version, expanded=False)
        if version != "1.0":
            raise XMLCapabilityError("XML versions other than 1.0")
        if encoding is not None:
            self.text(encoding, expanded=False)
            selected = encoding.casefold()
            if selected not in ("utf-8", "utf-16", "utf-16le", "utf-16be"):
                raise XMLCapabilityError("encoding outside UTF-8/UTF-16")
            if selected.startswith("utf-16") and self.encoding == "utf-8":
                raise XMLCapabilityError("UTF-16 without a byte-order mark")
        self.declaration_encoding = encoding
        self.declaration_seen = True

    def namespace(self, prefix: str | None, uri: str | None) -> None:
        """Charge actual expanded namespace bindings supplied by Expat."""
        self.event()
        self.budget.consume("states")
        self.text(prefix or "")
        self.text(uri or "")

    def end_namespace(self, prefix: str | None) -> None:
        """Bound namespace-scope callbacks without reconstructing their resolution."""
        self.event()
        self.text(prefix or "")

    def start(self, name: str, attributes: dict[str, str]) -> None:
        """Count expanded names and attributes, including XML base/language."""
        self.event()
        literal = "http://www.w3.org/1999/02/22-rdf-syntax-ns# parseType"
        if self.entities and attributes.get(literal) == "Literal":
            raise XMLCapabilityError("internal entities with RDF XMLLiteral content")
        self.depth += 1
        if self.depth > self.budget.limits.max_depth:
            raise LimitExceeded("XML nesting exceeds max_depth")
        self.budget.consume("states", len(attributes) + 1)
        self.text(name)
        for key, value in attributes.items():
            self.text(key)
            self.text(value)

    def end(self, name: str) -> None:
        """Keep closing markup work and cumulative content within the same bounds."""
        self.event()
        self.text(name)
        self.depth -= 1

    def data(self, value: str) -> None:
        """Measure entity-expanded character data and harmless comment contents."""
        self.event()
        self.text(value)

    def instruction(self, target: str, value: str) -> None:
        """Preserve ordinary processing instructions without interpreting them."""
        self.event()
        self.text(target)
        self.text(value)

    def doctype(
        self, name: str, system: str | None, public: str | None, internal: int
    ) -> None:
        """Allow only an internal declaration subset without any external resolver."""
        self.event()
        self.text(name, expanded=False)
        if system is not None or public is not None:
            raise XMLCapabilityError("external document type subset")
        self.in_dtd = True

    def end_doctype(self) -> None:
        """Leave the finite internal declaration scope."""
        self.event()
        self.in_dtd = False

    def entity(
        self,
        name: str,
        parameter: int,
        value: str | None,
        base: str | None,
        system: str | None,
        public: str | None,
        notation: str | None,
    ) -> None:
        """Accept internal general text entities; never create external parsers."""
        self.event()
        if parameter:
            raise XMLCapabilityError("parameter entity declaration")
        if (
            system is not None
            or public is not None
            or notation is not None
            or value is None
        ):
            raise XMLCapabilityError("external or unparsed entities")
        self.budget.consume("states")
        self.text(name, expanded=False)
        self.text(value, expanded=False)
        if "<" in value:
            raise XMLCapabilityError("entity replacement containing markup")
        self.entities += 1

    def default(self, value: str) -> None:
        """Reject opaque DTD tokens, including skipped/redeclared parameter input."""
        self.event()
        self.text(value)
        if self.in_dtd and value.strip():
            raise XMLCapabilityError("unhandled document type declaration")

    def unsupported_dtd(self, *values: object) -> None:
        """Prevent default attributes, element models, notations and unparsed data."""
        raise XMLCapabilityError("non-entity document type declaration")

    def skipped(self, name: str, parameter: int) -> None:
        """Skipped input cannot silently qualify an incomplete XML interpretation."""
        raise XMLCapabilityError("skipped entity reference")

    def external(
        self, context: str, base: str | None, system: str | None, public: str | None
    ) -> int:
        """Refuse every external entity request without fetching or opening a file."""
        raise XMLCapabilityError("external entity reference")

    def normalized(self) -> bytes:
        """Transcode UTF-16 while preserving body markup and character columns."""
        if self.encoding == "utf-8":
            return self.payload
        # UTF-16 code units bound Unicode storage before the maintained decoder.
        self.budget.consume("bytes", 64 + 2 * len(self.payload))
        self.budget.consume("work", len(self.payload))
        text = self.payload.decode("utf-16")
        if self.declaration_encoding is not None:
            assert self.prefix_end is not None
            upper = (self.prefix_end - 2) // 2
            if upper > self.max_bytes:
                raise LimitExceeded("XML declaration prefix exceeds max_bytes")
            self.budget.consume("work", upper)
            token = self.declaration_encoding
            start = text.find(token, 0, upper)
            end = start + len(token)
            if (
                start <= 0
                or text[start - 1] not in ("'", '"')
                or text[end] != text[start - 1]
            ):
                raise XMLCapabilityError("unqualified encoding declaration boundary")
            self.budget.consume("bytes", 64 + 4 * len(self.payload))
            # Expat established this exact encoding field. Padding after its quote
            # retains every later character/line position without scanning the body.
            text = "".join(
                (
                    text[:start],
                    "UTF-8",
                    text[end : end + 1],
                    " " * (len(token) - 5),
                    text[end + 1 :],
                )
            )
        size = 0
        self.budget.consume("work", len(text))
        for offset in range(0, len(text), 4096):
            size += len(text[offset : offset + 4096].encode("utf-8"))
            if size > self.max_bytes:
                raise LimitExceeded("Normalized XML exceeds max_bytes")
        self.budget.consume("bytes", size)
        return text.encode("utf-8")


def prepare_xml(
    payload: bytes, *, budget: Budget, max_bytes: int | None = None
) -> XMLInput:
    """Validate already-owned XML before native RDF parsing, without network access.

    XML 1.0 UTF-8 and BOM-qualified UTF-16 byte orders are supported. Safe internal
    general text entities remain authored declarations for the RDF parser;
    external/parameter entities, default attributes and other DTD constructs are
    explicit capability failures. Expat's native amplification protection stays
    enabled. Actual expanded text/attributes, nesting and normalized UTF-8 bytes
    share the caller's finite budget. Internal entities combined with actual
    RDF-namespace parseType=Literal are unsupported: the maintained RDF parser
    does not preserve entity-based namespace bindings inside XMLLiteral terms.
    No XML/RDF grammar is reconstructed.
    """
    if not isinstance(payload, bytes) or not isinstance(budget, Budget):
        raise InputError("XML preparation requires bytes and a shared Budget")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise InputError("XML max_bytes requires a positive integer")
    cap = min(
        max_bytes if max_bytes is not None else budget.limits.max_bytes,
        budget.limits.max_bytes,
    )
    if len(payload) > cap:
        raise LimitExceeded("XML input exceeds max_bytes")
    budget.consume("work", len(payload))
    boundary = _Boundary(payload, budget, cap)
    try:
        for offset in range(0, len(payload), 16_384):
            budget.consume("work")
            boundary.parser.Parse(payload[offset : offset + 16_384], False)
        boundary.parser.Parse(b"", True)
    except expat.ExpatError as error:
        if error.code == expat.errors.codes.get(
            expat.errors.XML_ERROR_AMPLIFICATION_LIMIT_BREACH
        ):
            raise LimitExceeded(
                "XML entity amplification exceeded native limits"
            ) from error
        raise ResourceError("Cannot parse RDF/XML input at the XML boundary") from error
    return XMLInput(
        boundary.normalized(), boundary.encoding, boundary.entities, boundary.expanded
    )
