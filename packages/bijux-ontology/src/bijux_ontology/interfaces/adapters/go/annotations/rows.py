"""Admit explicitly versioned GO headers and bounded original CSV row positions."""

import csv
import io

from bijux_ontology.diagnostics.errors import (
    InputError,
    LimitExceeded,
    ResourceError,
)
from bijux_ontology.diagnostics.reports import Issue, Location
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.limits import (
    IssueCollector,
    bound_text,
)
from bijux_ontology.rdf.identifiers import absolute_iri

from .syntax import PREFIX, calendar_date, source_error


def decode_rows(
    source: str, content: str, kind: str, operation: Operation, issues: IssueCollector
) -> tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]], bool]:
    """Bound text before CSV allocation and retain actual per-record locations."""
    if type(source) is not str or type(content) is not str:
        raise InputError("GO resources require ordinary source and content strings")
    bound_text(source, operation)
    absolute_iri(source)
    bound_text(content, operation)
    operation.consume("bytes", 12 * len(content) + 128)
    version = "2.2" if kind == "gaf" else "2.0"
    headers: list[str] = []
    rows: list[tuple[int, tuple[str, ...]]] = []
    mandatory: dict[str, str] = {}
    complete = True
    body = False
    for number, line in enumerate(io.StringIO(content), 1):
        operation.consume("work", len(line) + 1)
        operation.consume("states", 2)
        text = line.removesuffix("\n").removesuffix("\r")
        try:
            if not text:
                raise InputError("Blank lines are outside the supported record grammar")
            if kind != "gaf" and (
                not text.isascii() or any(ord(c) < 32 and c != "\t" for c in text)
            ):
                raise InputError("GPAD/GPI require printable ASCII fields")
            if text.startswith("!"):
                if kind != "gaf" and "\t" in text:
                    raise InputError("GO header text cannot contain tab separators")
                if body:
                    raise InputError("Headers must precede annotation/entity records")
                key, separator, value = text[1:].partition(":")
                if separator and key in (
                    kind + "-version",
                    "generated-by",
                    "date-generated",
                ):
                    if key in mandatory:
                        raise InputError("Required header must occur exactly once")
                    declared = value.removeprefix(" ")
                    if key == kind + "-version" and declared != version:
                        mandatory[key] = declared
                        raise ResourceError(
                            "Declared format version is outside this adapter profile"
                        )
                    if key == "generated-by" and not PREFIX.fullmatch(declared):
                        raise InputError("Invalid generated-by header prefix")
                    if key == "date-generated":
                        calendar_date(declared)
                    mandatory[key] = declared
                headers.append(text)
                continue
            body = True
            if kind + "-version" not in mandatory:
                raise InputError("Missing explicit supported format version")
            if mandatory[kind + "-version"] != version:
                raise ResourceError(
                    "Declared format version is outside this adapter profile"
                )
            if not PREFIX.fullmatch(
                mandatory.get("generated-by", "")
            ) or not mandatory.get("date-generated"):
                raise InputError("Missing generated-by/date-generated header")
            calendar_date(mandatory["date-generated"])
            expected = 17 if kind == "gaf" else 12 if kind == "gpad" else 11
            if text.count("\t") != expected - 1:
                raise InputError(f"Expected exactly {expected} tab-separated fields")
            operation.consume("states", expected + 1)
            try:
                values = tuple(
                    next(
                        csv.reader(
                            (text,), delimiter="\t", quoting=csv.QUOTE_NONE, strict=True
                        )
                    )
                )
            except csv.Error as error:
                raise ResourceError(
                    "CSV implementation could not preserve this bounded record"
                ) from error
            expected = 17 if kind == "gaf" else 12 if kind == "gpad" else 11
            if len(values) != expected:
                raise InputError(f"Expected exactly {expected} tab-separated fields")
            if any(any(ord(c) < 32 for c in field) for field in values):
                raise InputError(
                    "Control characters are outside the supported field grammar"
                )
            rows.append((number, values))
        except LimitExceeded:
            raise
        except InputError as error:
            complete = False
            qualified = source_error(str(error), source, number)
            issues.append(
                Issue(
                    "go.input_unverified"
                    if isinstance(error, ResourceError)
                    else "go.input_invalid",
                    str(error),
                    source,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                    details=Location.from_error(qualified),
                )
            )
    if not body and (
        mandatory.get(kind + "-version") != version
        or not mandatory.get("generated-by")
        or not mandatory.get("date-generated")
    ):
        complete = False
        located = source_error(
            "Required format header is missing or invalid", source, 1
        )
        issues.append(
            Issue(
                "go.input_unverified"
                if mandatory.get(kind + "-version") not in (None, version)
                else "go.input_invalid",
                str(located),
                source,
                kind="unverified"
                if mandatory.get(kind + "-version") not in (None, version)
                else "input_error",
                details=Location.from_error(located),
            )
        )
    return tuple(headers), rows, complete
