"""RFC 4647 range syntax and finite basic/extended filtering primitives."""

import re
from collections.abc import Iterator

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.identifiers import language_tag

_BASIC = re.compile(r"(?:[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*|\*)\Z")
_EXTENDED = re.compile(r"(?:[A-Za-z]{1,8}|\*)(?:-(?:[A-Za-z0-9]{1,8}|\*))*\Z")


def _text(value: str, budget: Budget) -> None:
    """Bound text before allocating normalized matching keys."""
    if not isinstance(value, str):
        raise InputError("Language matching requires text")
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Language matching text exceeds max_bytes")
    budget.consume("work", len(value))
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Language matching requires valid Unicode text") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Language matching text exceeds max_bytes")
    budget.consume("bytes", size)


def _range(value: str, extended: bool, budget: Budget) -> tuple[str, ...]:
    _text(value, budget)
    if (_EXTENDED if extended else _BASIC).fullmatch(value) is None:
        raise InputError("Invalid RFC 4647 language range for the selected dialect")
    budget.consume("states", value.count("-") + 1)
    return tuple(value.lower().split("-"))


def _tag(value: str | None, budget: Budget) -> tuple[str, ...] | None:
    if value is None:
        return None
    _text(value, budget)
    language_tag(value)
    budget.consume("states", value.count("-") + 1)
    return tuple(value.lower().split("-"))


def _matches(
    tag: tuple[str, ...] | None,
    wanted: tuple[str, ...],
    extended: bool,
    budget: Budget,
) -> bool:
    if tag is None:
        return False
    budget.consume("work")
    if wanted == ("*",):
        return True
    if not extended:
        budget.consume("work", len(wanted))
        return len(tag) >= len(wanted) and tag[: len(wanted)] == wanted
    if wanted[0] != "*" and wanted[0] != tag[0]:
        return False
    position = 1
    for part in wanted[1:]:
        budget.consume("work")
        if part == "*":
            continue
        while position < len(tag):
            budget.consume("work")
            actual = tag[position]
            position += 1
            if actual == part:
                break
            if len(actual) == 1:
                return False
        else:
            return False
    return True


def _lookup(wanted: tuple[str, ...], budget: Budget) -> Iterator[tuple[str, ...]]:
    """Truncate ranges, removing a trailing singleton with its last extension."""
    if wanted == ("*",):
        return
    while wanted:
        budget.consume("work", len(wanted))
        yield wanted
        wanted = wanted[:-1]
        while wanted and len(wanted[-1]) == 1:
            budget.consume("work", len(wanted))
            wanted = wanted[:-1]


def matches_language(
    tag: str | None,
    language_range: str,
    *,
    extended: bool = False,
    budget: Budget | None = None,
) -> bool:
    """Apply RFC 4647 basic or extended filtering to one maintained RDF tag.

    Untagged values never match a range, including '*'. This predicate does not
    perform lookup fallback, lexical comparison, registry canonicalization or
    annotation coverage. Ranges follow RFC range syntax, which is distinct from
    well-formed language tags; actual tags reuse maintained RDF validation.
    """
    if (
        type(extended) is not bool
        or budget is not None
        and not isinstance(budget, Budget)
    ):
        raise InputError("Expected a boolean dialect and optional Budget")
    operation = budget if budget is not None else Budget()
    wanted = _range(language_range, extended, operation)
    return _matches(_tag(tag, operation), wanted, extended, operation)
