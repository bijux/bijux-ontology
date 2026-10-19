"""Bounded maintained RE2 predicates shared by RDF-facing value checks."""

from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast

from bijux_ontology.execution.limits import Budget

_PATTERN_MEMORY = 65_536


class _Pattern(Protocol):
    """The two maintained public RE2 operations used for boolean predicates."""

    def fullmatch(self, text: str) -> object:
        """Match the entire original text with maintained UTF-8 semantics."""
        ...

    def search(self, text: str) -> object:
        """Match a substring of the original text without rewriting it."""
        ...


def matches_pattern(pattern: str, text: str, mode: str, budget: Budget) -> bool | None:
    """Use maintained bounded RE2 syntax, without a backtracking fallback.

    The public wrapper caches at most 128 compiled expressions. A fixed 64 KiB
    approximate RE2 cap limits our entries' engine allowance to 8 MiB;
    it is not a process RSS guarantee. Program/input work is charged first.
    """
    budget.consume("work", (len(pattern) + 1) * (len(text) + 1))
    try:
        release = version("google-re2")
        numbers = tuple(int(part) for part in release.split("."))
        if numbers < (1, 1, 20251105) or numbers[0] != 1:
            return None
        import re2  # type: ignore[import-untyped]
    except (PackageNotFoundError, ImportError, ValueError):
        return None
    options = re2.Options()
    options.max_mem = _PATTERN_MEMORY
    options.log_errors = False
    options.never_capture = True
    try:
        compiled = cast(_Pattern, re2.compile(pattern, options=options))
        result = (
            compiled.fullmatch(text) if mode == "fullmatch" else compiled.search(text)
        )
    except re2.error:
        return None
    return result is not None
