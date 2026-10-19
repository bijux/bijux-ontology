"""Bounded application predicates over an already established actual RDF term."""

from decimal import Decimal, InvalidOperation
from sys import getsizeof

from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD
from rdflib.term import Node

from bijux_ontology.declarations.enumerations import ValueEnumeration
from bijux_ontology.declarations.value_constraints import ValueConstraints
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, bound_text
from bijux_ontology.rdf.literals.checking import check_literal, datatype_relation
from bijux_ontology.rdf.literals.patterns import matches_pattern
from bijux_ontology.rdf.terms import term_key


def _text(value: str, budget: Budget) -> str:
    """Bound each scalar and cumulative retained comparison text."""
    bound_text(value, budget)
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


def _valid_literal(term: Literal, budget: Budget, known: bool | None) -> bool:
    """Reuse a supplied lexical proof or fund qualification on the same allowance."""
    if known is not None:
        return known
    report = check_literal(term, budget=budget)
    if report.outcome == "exhausted":
        for issue in report.issues:
            if issue.kind == "exhausted":
                raise LimitExceeded(issue.message)
        raise LimitExceeded("Application literal qualification exhausted its budget")
    return report.verified


def _number(term: Node, budget: Budget, lexical_valid: bool | None) -> Decimal | None:
    """Interpret only validated integer/decimal lexical forms exactly."""
    if not isinstance(term, Literal) or term.datatype is None:
        return None
    if datatype_relation(
        str(term.datatype), str(XSD.decimal), regime="application"
    ) not in ("equal", "subset"):
        return None
    if not _valid_literal(term, budget, lexical_valid):
        return None
    budget.consume("work", len(str(term)) + 1)
    try:
        value = Decimal(str(term))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def _string(term: Node, budget: Budget, lexical_valid: bool | None) -> str | None:
    """Use string value spelling without arbitrary typed-literal conversion."""
    if not isinstance(term, Literal):
        return None
    datatype = str(term.datatype or (RDF.langString if term.language else XSD.string))
    if datatype != str(RDF.langString) and datatype_relation(
        datatype, str(XSD.string)
    ) not in ("equal", "subset"):
        return None
    valid = _valid_literal(term, budget, lexical_valid)
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
    if constraints.enumeration == () or (
        isinstance(constraints.enumeration, ValueEnumeration)
        and constraints.enumeration.qualified
        and not constraints.enumeration.values
    ):
        return False
    if actual is None:
        return True if constraints == ValueConstraints() else None
    actual_key = _term(actual, budget)
    results: list[bool | None] = []
    if constraints.enumeration is not None:
        enumeration = constraints.enumeration
        allowed = (
            enumeration.values
            if isinstance(enumeration, ValueEnumeration)
            else enumeration
        )
        forbidden = (
            enumeration.excluded if isinstance(enumeration, ValueEnumeration) else ()
        )
        qualified = (
            enumeration.qualified if isinstance(enumeration, ValueEnumeration) else True
        )
        found = False
        excluded = False
        for terms, is_excluded in ((allowed, False), (forbidden, True)):
            if len(terms) > budget.limits.max_states:
                raise LimitExceeded("Value enumeration exceeds max_states")
            for index in range(len(terms)):
                budget.consume("work")
                budget.consume("states")
                if _term(terms[index], budget) == actual_key:
                    if is_excluded:
                        excluded = True
                    else:
                        found = True
        results.append(
            False if excluded else True if found else False if qualified else None
        )
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
        text = _string(actual, budget, lexical_valid)
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
                    matches_pattern(pattern, text, constraints.pattern_mode, budget)
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


def retain_enumeration_report(
    constraints: ValueConstraints | None,
    reports: dict[tuple[int, bool], Report],
    *,
    budget: Budget,
    issues: IssueCollector,
) -> None:
    """Preserve each required selection report once in this checking operation.

    Retain child diagnostics with the current allowance before attaching the
    entire immutable report. A limit keeps proved finding prefixes and prevents
    a large externally prepared report from bypassing this operation's budget.
    """
    enumeration = constraints.enumeration if constraints is not None else None
    if not isinstance(enumeration, ValueEnumeration):
        return
    key = (
        id(enumeration.report) if enumeration.report is not None else id(enumeration),
        enumeration.closed,
    )
    if key in reports:
        return
    budget.consume("work")
    budget.consume("states")
    report = enumeration.qualification_report()
    issues.extend(report.issues)
    reports[key] = report
