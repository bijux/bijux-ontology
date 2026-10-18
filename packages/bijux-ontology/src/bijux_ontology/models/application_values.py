"""Bounded application predicates over an already established actual RDF term."""

from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from sys import getsizeof
from typing import Protocol, cast

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.literals import check_literal, datatype_relation
from bijux_ontology.rdf.terms import term_key

_PATTERN_MEMORY = 65_536


class _Pattern(Protocol):
    """The two maintained public RE2 operations used for boolean predicates."""

    def fullmatch(self, text: str) -> object:
        """Match the entire original text with maintained UTF-8 semantics."""
        ...

    def search(self, text: str) -> object:
        """Match a substring of the original text without rewriting it."""
        ...


def _text(value: str, budget: Budget) -> str:
    """Bound each scalar and cumulative retained comparison text."""
    if len(value) > budget.limits.max_bytes:
        raise LimitExceeded("Application value text exceeds max_bytes")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InputError("Application values require valid Unicode") from error
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Application value text exceeds max_bytes")
    budget.consume("bytes", size)
    budget.consume("work", len(value) + 1)
    return value


def _term(term: Node, budget: Budget) -> tuple[str | None, ...]:
    """Charge original term metadata before deriving an exact immutable key."""
    _text(str(term), budget)
    if isinstance(term, Literal):
        if term.datatype is not None:
            _text(str(term.datatype), budget)
        if term.language is not None:
            _text(term.language, budget)
    return term_key(term)


def _pattern(pattern: str, text: str, mode: str, budget: Budget) -> bool | None:
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


def _number(term: Node, budget: Budget, lexical_valid: bool | None) -> Decimal | None:
    """Interpret only validated integer/decimal lexical forms exactly."""
    if not isinstance(term, Literal) or term.datatype is None:
        return None
    if datatype_relation(
        str(term.datatype), str(XSD.decimal), regime="application"
    ) not in ("equal", "subset"):
        return None
    if not (check_literal(term).verified if lexical_valid is None else lexical_valid):
        return None
    budget.consume("work", len(str(term)) + 1)
    try:
        value = Decimal(str(term))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _string(term: Node, lexical_valid: bool | None) -> str | None:
    """Use string value spelling without arbitrary typed-literal conversion."""
    if not isinstance(term, Literal):
        return None
    datatype = str(term.datatype or (RDF.langString if term.language else XSD.string))
    if datatype != str(RDF.langString) and datatype_relation(
        datatype, str(XSD.string)
    ) not in ("equal", "subset"):
        return None
    valid = check_literal(term).verified if lexical_valid is None else lexical_valid
    return str(term) if valid else None


def application_matches(
    constraints: ValueConstraints,
    actual: Node | None,
    *,
    budget: Budget,
    lexical_valid: bool | None = None,
) -> bool | None:
    """Conjoin explicit scalar predicates without traversing or changing RDF.

    None represents unavailable exact term correspondence, never an invented
    literal or blank identifier. Unsupported syntax/conversion remains unknown;
    a separately established predicate violation still returns False. Callers
    may reuse an existing lexical report; otherwise conversion is qualified here.
    """
    if not isinstance(constraints, ValueConstraints):
        raise InputError("Expected explicit application value constraints")
    budget.consume("work")
    if constraints.enumeration == ():
        return False
    if actual is None:
        return True if constraints == ValueConstraints() else None
    actual_key = _term(actual, budget)
    results: list[bool | None] = []
    if constraints.enumeration is not None:
        count = len(constraints.enumeration)
        if count > budget.limits.max_states:
            raise LimitExceeded("Value enumeration exceeds max_states")
        found = False
        for index in range(count):
            budget.consume("work")
            budget.consume("states")
            if _term(constraints.enumeration[index], budget) == actual_key:
                found = True
        results.append(found)
    if constraints.minimum is not None or constraints.maximum is not None:
        for bound in (constraints.minimum, constraints.maximum):
            if bound is not None:
                if getsizeof(bound, 0) > budget.limits.max_bytes:
                    raise LimitExceeded("Numeric bound exceeds max_bytes")
                budget.consume("bytes", getsizeof(bound, 0))
                _text(str(bound), budget)
        number = _number(actual, budget, lexical_valid)
        if number is None:
            results.append(None)
        else:
            results.append(
                (
                    constraints.minimum is None
                    or number > constraints.minimum
                    or constraints.minimum_inclusive
                    and number == constraints.minimum
                )
                and (
                    constraints.maximum is None
                    or number < constraints.maximum
                    or constraints.maximum_inclusive
                    and number == constraints.maximum
                )
            )
    if (
        constraints.min_length is not None
        or constraints.max_length is not None
        or constraints.pattern is not None
    ):
        text = _string(actual, lexical_valid)
        if text is None:
            results.append(None)
        else:
            if constraints.min_length is not None:
                results.append(len(text) >= constraints.min_length)
            if constraints.max_length is not None:
                results.append(len(text) <= constraints.max_length)
            if constraints.pattern is not None:
                pattern = _text(constraints.pattern, budget)
                results.append(
                    _pattern(pattern, text, constraints.pattern_mode, budget)
                )
    if constraints.iri_namespaces:
        count = len(constraints.iri_namespaces)
        if count > budget.limits.max_states:
            raise LimitExceeded("IRI namespace selection exceeds max_states")
        found = False
        for index in range(count):
            budget.consume("work")
            budget.consume("states")
            namespace = _text(constraints.iri_namespaces[index], budget)
            if isinstance(actual, URIRef) and str(actual).startswith(namespace):
                found = True
        results.append(found)
    return False if False in results else None if None in results else True
