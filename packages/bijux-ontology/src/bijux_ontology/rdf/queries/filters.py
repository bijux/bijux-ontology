"""Preserve lone FILTER clauses at the maintained RDFLib execution boundary."""

from collections.abc import Sequence

from rdflib import Literal
from rdflib.plugins.sparql.parserutils import CompValue
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.execution.limits import Budget


def capture_lone_filter(group: CompValue, budget: Budget) -> CompValue | None:
    """Retain the actual node before native translation removes group filters.

    Call from the existing bounded parse-tree traversal only for RDFLib execution.
    Multiple filters produce a maintained expression and do not take its lossy
    single-Literal branch. No query expression is interpreted or rewritten.
    """
    if group.name != "GroupGraphPatternSub":
        return None
    only = None
    # CompValue.get's second argument controls evaluation, not missing keys.
    for child in group["part"] if "part" in group else ():  # noqa: SIM401
        budget.consume("work")
        if isinstance(child, CompValue) and child.name == "Filter":
            if only is not None:
                return None
            only = child
    if only is not None:
        budget.consume("states")
        budget.reserve_storage(8)
    return only


def require_filter_preservation(
    filters: Sequence[CompValue], budget: Budget, *, location: str | Node = ""
) -> None:
    """Reject a known native omission after the same nodes were translated.

    RDFLib simplifies original Filter nodes in place, then drops a lone Literal
    through a Python truth-value test. Syntax-only users of translated algebra
    and capable native engines must not inherit this execution limitation.
    """
    for original in filters:
        budget.consume("work")
        expression = original["expr"]
        if isinstance(expression, Literal) and not expression:
            raise ResourceError(
                "RDFLib cannot preserve a lone false-valued constant FILTER",
                location=str(location),
            )
