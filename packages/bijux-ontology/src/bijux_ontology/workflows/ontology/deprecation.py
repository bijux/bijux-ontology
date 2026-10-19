"""Selected obsolete-term policy over shared deprecation inspection conventions."""

from collections.abc import Sequence

from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.builtins import Regime
from bijux_ontology.vocabulary.deprecation import replacement_candidates


def check_deprecated_terms(
    view: VocabularyView,
    used_terms: Sequence[str],
    replacement_predicates: Sequence[str],
    regime: Regime,
    budget: Budget,
    issues: IssueCollector,
) -> int:
    """Retain all supplied hints and their limitations without choosing a winner."""
    checked = 0
    for iri in used_terms:
        budget.consume("work")
        state = view.deprecation(
            iri, replacement_predicates=replacement_predicates, budget=budget
        )
        list.extend(issues, state.report.issues)
        checked += 1
        if state.state in ("conflicting", "unverified"):
            issues.append(
                Issue(
                    "ontology.deprecation_unverified",
                    "Selected deprecation metadata is conflicting or unsupported",
                    iri,
                    kind="unverified",
                )
            )
        elif state.state == "deprecated":
            hints = tuple(
                sorted(
                    {
                        edge.object.n3(): edge.object for edge in state.replacements
                    }.values(),
                    key=str,
                )
            )
            issues.append(
                Issue(
                    "ontology.deprecated_term",
                    "Application uses a declared deprecated term",
                    iri,
                    severity="warning",
                    actual=", ".join(map(str, hints)) or None,
                )
            )
            if replacement_predicates and not hints:
                issues.append(
                    Issue(
                        "ontology.replacement_absent",
                        "No configured replacement hint is supplied",
                        iri,
                        severity="info",
                    )
                )
            elif len(hints) > 1:
                issues.append(
                    Issue(
                        "ontology.replacement_ambiguous",
                        "Multiple hints require a consumer decision; none was selected",
                        iri,
                        severity="info",
                        actual=", ".join(map(str, hints)),
                    )
                )
            candidates = replacement_candidates(
                view,
                iri,
                predicates=replacement_predicates,
                regime=regime,
                budget=budget,
            )
            list.extend(issues, candidates.report.issues)
    return checked
