"""Explicit trusted Python rules over an isolated RDF view."""

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace

from rdflib import Graph
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.snapshots import copy_graph


class DataView:
    """Expose immutable RDF terms and owned graph exports to trusted callbacks."""

    def __init__(self, graph: Graph) -> None:
        self._graph = copy_graph(graph)

    def triples(
        self, pattern: tuple[Node | None, Node | None, Node | None] = (None, None, None)
    ) -> Iterator[tuple[Node, Node, Node]]:
        """Iterate matching immutable term tuples without exposing a mutable graph."""
        yield from self._graph.triples(pattern)

    def graph(self) -> Graph:
        """Return an independently mutable graph with stable term identities."""
        result = Graph()
        result += self._graph
        return result


PythonRule = Callable[[DataView], Report | Iterable[Issue]]


def run_rules(
    data: Graph,
    rules: Mapping[str, PythonRule],
    *,
    limits: Limits | None = None,
) -> Report:
    """Run trusted callables with bounded owned result collection.

    A returned issue iterable represents one completed rule obligation; a Report
    retains its own scope. Callback and iterator exceptions propagate unchanged.
    The budget cannot bound arbitrary trusted callback CPU or side effects.
    No dynamic discovery or configuration expressions are evaluated.
    """
    selected = limits if limits is not None else Limits()
    budget = Budget(selected)
    reports: list[Report] = []
    try:
        budget.consume("triples", len(data))
    except LimitExceeded as error:
        return Report(
            "data-rules",
            0,
            (Issue("execution.limit_exceeded", str(error), kind="exhausted"),),
            complete=False,
        )
    for name, rule in rules.items():
        if not isinstance(name, str) or not name.strip() or not callable(rule):
            raise InputError("Rules require nonempty names and explicit callables")
        try:
            budget.consume("work", max(1, len(data)))
        except LimitExceeded as error:
            reports.append(
                Report(
                    name,
                    0,
                    (Issue("execution.limit_exceeded", str(error), kind="exhausted"),),
                    complete=False,
                )
            )
            break
        result = rule(DataView(data))
        retained = IssueCollector(budget)
        emitted = result.issues if isinstance(result, Report) else result
        complete = True
        # Advancing the trusted iterator is deliberately outside the budget
        # exception handler, so its own exceptions are never reclassified.
        for issue in emitted:
            if not isinstance(issue, Issue):
                raise InputError(
                    "A Python rule must return Issues or a Report", location=name
                )
            try:
                budget.consume("work")
                retained.append(issue)
            except LimitExceeded as error:
                retained.exhausted(str(error), location=name)
                complete = False
                break
        if isinstance(result, Report):
            reports.append(
                result
                if complete
                else replace(
                    result, issues=tuple(retained), complete=False, children=()
                )
            )
        else:
            reports.append(
                Report(
                    name,
                    int(complete),
                    tuple(retained),
                    complete=complete,
                    families=("trusted-python",),
                )
            )
        if not complete:
            break
    return combine_reports("data-rules", *reports)
