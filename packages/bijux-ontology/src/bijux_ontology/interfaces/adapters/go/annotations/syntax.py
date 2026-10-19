"""Exact GO field grammars and original source coordinates shared by row roles."""

import re
from datetime import date, datetime

from bijux_ontology.diagnostics.errors import (
    InputError,
    InputFailure,
    SourceSpan,
)
from bijux_ontology.execution.budgets import Operation

from .types import (
    GoExtension,
)

CURIE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_.:/-]+\Z", re.ASCII)


GAF_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[^\s|]+\Z")


PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z", re.ASCII)


RELATION = re.compile(r"[a-z][a-z_]*\Z", re.ASCII)


DATE_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?\Z",
    re.ASCII,
)


def source_error(message: str, source: str, line: int, column: int = 1) -> InputError:
    """Attach actual domain-parser source coordinates, not inferred RDF positions."""
    return InputError(
        message,
        failure=InputFailure(
            document_iri=source,
            location=source,
            role="go-annotation",
            span=SourceSpan(
                start_line=line, start_column=column, column_unit="unicode_code_points"
            ),
        ),
    )


def identifier(value: str, *, strict: bool = True) -> str:
    """Accept the official finite ID grammar without changing its spelling."""
    if not (CURIE if strict else GAF_ID).fullmatch(value):
        raise InputError("Identifier does not match the supported GO CURIE grammar")
    return value


def calendar_date(value: str, *, compact: bool = False) -> None:
    """Use maintained Gregorian date parsing after the exact format guard."""
    if compact:
        if len(value) != 8 or not value.isascii() or not value.isdigit():
            raise InputError("GAF dates require YYYYMMDD")
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    elif not DATE_TIME.fullmatch(value):
        raise InputError("GO dates require the supported ISO date or dateTime grammar")
    try:
        datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    except ValueError as error:
        raise InputError("Invalid Gregorian date or dateTime") from error


def group_items(
    value: str,
    operation: Operation,
    *,
    required: bool = False,
    identifiers: bool = True,
    strict: bool = True,
) -> tuple[str, ...]:
    """Retain each pipe position, including duplicates, with no set coercion."""
    if not value:
        if required:
            raise InputError("A required field is empty")
        return ()
    operation.consume("states", value.count("|") + 1)
    operation.consume("bytes", 4 * len(value) + 64 * (value.count("|") + 1))
    values = tuple(value.split("|"))
    for item in values:
        operation.consume("work", len(item) + 1)
        if not item:
            raise InputError("An empty grouped field item is invalid")
        if identifiers:
            identifier(item, strict=strict)
    return values


def logical_groups(
    value: str,
    operation: Operation,
    *,
    extension: bool = False,
    curie_relation: bool = True,
    strict: bool = True,
) -> tuple[tuple[str | GoExtension, ...], ...]:
    """Preserve disjunction of conjunctions under the official flat grammar."""
    if not value:
        return ()
    operation.consume("states", 2 * (len(value) + 1))
    operation.consume(
        "bytes", 8 * len(value) + 64 * (value.count("|") + value.count(",") + 1)
    )
    groups: list[tuple[str | GoExtension, ...]] = []
    for conjunction in value.split("|"):
        elements: list[str | GoExtension] = []
        for item in conjunction.split(","):
            operation.consume("work", len(item) + 1)
            if not item:
                raise InputError("Empty conjunction/disjunction items are invalid")
            if extension:
                relation, separator, target = item.partition("(")
                if (
                    not separator
                    or not target.endswith(")")
                    or "(" in target
                    or ")" in target[:-1]
                ):
                    raise InputError(
                        "Extensions require relation(target) without nesting"
                    )
                if curie_relation:
                    identifier(relation)
                elif not RELATION.fullmatch(relation):
                    raise InputError(
                        "GAF extension relations require explicit relation labels"
                    )
                elements.append(
                    GoExtension(relation, identifier(target[:-1], strict=strict))
                )
            else:
                elements.append(identifier(item, strict=strict))
        groups.append(tuple(elements))
    return tuple(groups)


def check_properties(value: str, operation: Operation) -> None:
    """Keep property text while rejecting malformed finite key/value syntax."""
    for item in group_items(value, operation, identifiers=False):
        key, separator, text = item.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9-]+", key, re.ASCII)
            or not text
        ):
            raise InputError("Properties require nonempty key=value pairs")
