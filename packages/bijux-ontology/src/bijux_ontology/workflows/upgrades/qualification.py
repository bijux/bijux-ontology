"""Required consumer scope beside retained raw anonymous correspondence reports."""

from dataclasses import replace

from bijux_ontology.errors import LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.rdf.terms import term_key
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.comparison import VocabularyChanges
from bijux_ontology.vocabulary.facts.types import SelectedFact

_ATTRIBUTION = "vocabulary.fact_attribution_unverified"


def _key(fact: SelectedFact, budget: Budget) -> tuple[object, ...]:
    """Compare original owned facts; side-local blank keys never cross versions."""
    budget.consume("work")
    budget.consume("states")
    return (
        fact.source_scope,
        fact.graph.kind,
        term_key(fact.graph.name),
        tuple(term_key(n) for n in fact.statement),
    )


def _consumer_change_report(changes: VocabularyChanges, budget: Budget) -> Report:
    """Discharge only attachment uncertainty covered by complete neighborhoods.

    Exact per-blank fact deltas remain raw evidence on VocabularyChanges. The
    finite consumer assessment requires complete attachment attribution instead;
    its separate use join retains uncertainty for relevant or unowned changes.
    Role, graph, collection and resource failures remain required throughout.
    """
    raw = changes.report
    if not any(i.code == _ATTRIBUTION for i in raw.issues):
        return raw
    selected, anonymous = changes.selected_facts, changes.anonymous
    if selected is None or anonymous is None or not anonymous.report.complete:
        return raw
    covered: tuple[set[tuple[object, ...]], set[tuple[object, ...]]] = (set(), set())
    for side, facts in enumerate((anonymous.before, anonymous.after)):
        for fact in facts:
            covered[side].add(_key(fact, budget))
    for comparison in selected.anonymous:
        if not comparison.report.complete:
            return raw
        if comparison.report.outcome != "disagreement":
            continue
        for side, facts in enumerate((comparison.before, comparison.after)):
            for fact in facts:
                if _key(fact, budget) not in covered[side]:
                    return raw

    def qualify(report: Report) -> Report:
        """Recompute only the resolved attachment layer, keeping every other issue."""
        budget.consume("work")
        return replace(
            report,
            issues=tuple(i for i in report.issues if i.code != _ATTRIBUTION),
            children=tuple(qualify(c) for c in report.children),
            complete=True,
        )

    qualified = qualify(raw)
    return replace(
        qualified,
        scope="upgrade.changed_assertions",
        regime="finite-consumer-attachment-scope",
    )


def consumer_change_report(changes: VocabularyChanges, budget: Budget) -> Report:
    """Retain completed differences when scope qualification exhausts its budget."""
    try:
        return _consumer_change_report(changes, budget)
    except LimitExceeded as error:
        issues = IssueCollector(budget)
        issues.exhausted(str(error), location="upgrade.change_qualification")
        return replace(
            changes.report, issues=(*changes.report.issues, *issues), complete=False
        )
