"""Established artifact, pointer and semantic locations without guessed coordinates."""

from pathlib import Path
from urllib.parse import quote, urlsplit

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, bound_text
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Location


def _quoted(text: str, safe: str, budget: Budget) -> str:
    """Bound escaped URI length before allocating the complete representation."""
    budget.consume("work", len(text) + 1)
    size = 0
    for start in range(0, len(text), 4096):
        size += len(quote(text[start : start + 4096], safe=safe))
        if size > budget.limits.max_bytes:
            raise LimitExceeded("SARIF artifact URI exceeds max_bytes")
    budget.consume("bytes", size)
    return quote(text, safe=safe)


def _file_uri(path: Path, budget: Budget) -> str:
    """Prequalify pathname escaping before invoking the maintained URI formatter."""
    escaped = _quoted(str(path), "/:", budget)
    if len(escaped) + 12 > budget.limits.max_bytes:
        raise LimitExceeded("SARIF artifact URI exceeds max_bytes")
    result = path.as_uri()
    bound_text(result, budget)
    return result


def artifact_location(
    source: str, root: Path | None, budget: Budget
) -> dict[str, object]:
    """Preserve declared URIs; resolve filesystem references against the caller root."""
    bound_text(source, budget)
    try:
        parsed = urlsplit(source)
    except ValueError as error:
        raise InputError(
            "SARIF source requires a usable URI or file reference"
        ) from error
    if parsed.scheme:
        try:
            absolute_iri(source)
        except InputError as error:
            raise InputError("SARIF source contains an invalid absolute IRI") from error
        return {"uri": _quoted(source, "/:?#[]@!$&'()*+,;=%", budget)}
    if root is None:
        return {
            "uri": _file_uri(Path(source), budget)
            if Path(source).is_absolute()
            else _quoted(source, "/", budget)
        }
    path = Path(source)
    absolute = path.absolute() if path.is_absolute() else root / path
    bound_text(str(absolute), budget)
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        return {"uri": _file_uri(absolute, budget)}
    return {
        "uri": _quoted(relative.as_posix(), "/", budget),
        "uriBaseId": "ARTIFACT_ROOT",
    }


def pointer(path: tuple[str | int, ...], budget: Budget) -> str:
    """Escape only established source keys and indices, with a preallocation bound."""
    size = 0
    for part in path:
        budget.consume("work")
        if isinstance(part, int) and part.bit_length() > min(
            4096, budget.limits.max_bytes
        ):
            raise LimitExceeded("SARIF pointer index exceeds max_bytes")
        text = str(part)
        bound_text(text, budget)
        budget.consume("work", len(text) * 2)
        size += 1 + len(text) + text.count("~") + text.count("/")
    if size > budget.limits.max_bytes:
        raise LimitExceeded("SARIF pointer exceeds max_bytes")
    result = "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)
    bound_text(result, budget)
    return result


def location_data(
    details: Location | None,
    display: str,
    root: Path | None,
    budget: Budget,
) -> dict[str, object] | None:
    """Keep JSON pointers and RDF/model identities separate from physical regions."""
    budget.consume("states")
    result: dict[str, object] = {}
    logical: list[dict[str, object]] = []
    if details is not None:
        if not isinstance(details, Location):
            raise InputError("SARIF locations require typed native Location evidence")
        budget.consume("states", len(details.path))
        for text in (details.source, details.term, details.graph, details.model):
            if text is not None:
                bound_text(text, budget)
        if details.failure is not None:
            failure = details.failure
            budget.consume("states", len(failure.import_chain))
            for text in (
                failure.document_iri or "",
                failure.location,
                failure.role,
                *failure.import_chain,
            ):
                bound_text(text, budget)
        result["properties"] = {"bijux": {"location": details.to_dict()}}
        source = (
            details.failure.document_iri
            if details.failure is not None and details.failure.document_iri
            else details.source
        )
        if source:
            physical: dict[str, object] = {
                "artifactLocation": artifact_location(source, root, budget)
            }
            if details.failure is not None and details.failure.span is not None:
                span = details.failure.span
                for coordinate in span.to_dict().values():
                    if type(coordinate) is int and coordinate.bit_length() > min(
                        4096, budget.limits.max_bytes
                    ):
                        raise LimitExceeded("SARIF parser coordinate exceeds max_bytes")
                region: dict[str, object] = {}
                if span.start_line is not None:
                    region["startLine"] = span.start_line
                    if span.end_line is not None and span.end_line >= span.start_line:
                        region["endLine"] = span.end_line
                    if span.column_unit == "unicode_code_points":
                        if span.start_column is not None:
                            region["startColumn"] = span.start_column
                        if (
                            span.start_column is not None
                            and span.end_column is not None
                            and span.end_line is not None
                            and span.end_line >= span.start_line
                            and (
                                span.end_line > span.start_line
                                or span.end_column >= span.start_column
                            )
                        ):
                            region["endColumn"] = span.end_column
                if region:
                    physical["region"] = region
            result["physicalLocation"] = physical
        if details.path:
            logical.append(
                {
                    "fullyQualifiedName": pointer(details.path, budget),
                    "properties": {"bijux": {"kind": "json-pointer"}},
                }
            )
        for kind, value in (
            ("rdf-term", details.term),
            ("rdf-graph", details.graph),
            ("model", details.model),
        ):
            if value is not None:
                bound_text(value, budget)
                logical.append(
                    {
                        "fullyQualifiedName": value,
                        "properties": {"bijux": {"kind": kind}},
                    }
                )
    if "physicalLocation" not in result and not logical and display:
        bound_text(display, budget)
        logical.append(
            {
                "fullyQualifiedName": display,
                "properties": {"bijux": {"kind": "native-display"}},
            }
        )
    if logical:
        budget.consume("states", len(logical))
        result["logicalLocations"] = logical
    return result or None


def artifact_root(root: Path | None, budget: Budget) -> Path | None:
    """Qualify caller-owned filesystem metadata before deriving its absolute URI."""
    if root is None:
        return None
    if not isinstance(root, Path):
        raise InputError("SARIF artifact_root requires a Path")
    bound_text(str(root), budget)
    absolute = root.absolute()
    bound_text(str(absolute), budget)
    return absolute


def root_uri(root: Path, budget: Budget) -> str:
    """Qualify an absolute directory URI before adding its trailing separator."""
    result = _file_uri(root, budget)
    if len(result) + 1 > budget.limits.max_bytes:
        raise LimitExceeded("SARIF artifact root exceeds max_bytes")
    return result.rstrip("/") + "/"
