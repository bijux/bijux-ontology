"""Finite required sample coverage and binding-local evidence ownership."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from bijux_ontology.declarations.types import EmissionCondition
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget
from bijux_ontology.rdf.terms import term_key

TermKey = tuple[str | None, ...]


@dataclass(frozen=True)
class BranchCoverage:
    """Require an alternative by its declared zero-based field branch index."""

    model: str
    field: str
    branch: int
    discriminator: EmissionCondition | None = None

    def __post_init__(self) -> None:
        if any(
            not isinstance(item, str) or not item.strip()
            for item in (self.model, self.field)
        ):
            raise InputError("Branch coverage requires model and field identifiers")
        if type(self.branch) is not int or self.branch < 0:
            raise InputError("Branch coverage requires a nonnegative branch index")
        if self.discriminator is not None and not isinstance(
            self.discriminator, EmissionCondition
        ):
            raise InputError("Expected a typed branch discriminator")


@dataclass(frozen=True)
class ScenarioCoverage:
    """Require one explicitly declared emission-group scenario."""

    model: str
    group: str
    scenario: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(item, str) or not item.strip()
            for item in (self.model, self.group, self.scenario)
        ):
            raise InputError(
                "Scenario coverage requires model, group and scenario identifiers"
            )


class _CorpusEvidence:
    """Collect already resolved observations without reopening JSON-LD scope."""

    def __init__(
        self,
        branches: tuple[BranchCoverage, ...],
        scenarios: tuple[ScenarioCoverage, ...],
        budget: Budget,
    ) -> None:
        self.branches = branches
        self.scenario_requirements = scenarios
        self.budget = budget
        self.branch_witnesses: dict[BranchCoverage, list[str]] = {
            item: [] for item in branches
        }
        self.scenario_witnesses: dict[ScenarioCoverage, list[str]] = {
            item: [] for item in scenarios
        }
        self._by_field: dict[tuple[str, str], list[BranchCoverage]] = {}
        self._fields: dict[str, set[str]] = {}
        self._scenarios: dict[tuple[str, str], list[ScenarioCoverage]] = {}
        for branch in branches:
            budget.consume("work")
            self._by_field.setdefault((branch.model, branch.field), []).append(branch)
            if branch.discriminator is not None:
                self._fields.setdefault(branch.model, set()).add(
                    branch.discriminator.field
                )
        for scenario in scenarios:
            budget.consume("work")
            self._scenarios.setdefault((scenario.model, scenario.group), []).append(
                scenario
            )
        self.case = ""
        self.pending: dict[
            int, tuple[object, list[tuple[str, str, tuple[bool | None, ...]]]]
        ] = {}

    def begin(self, identifier: str) -> None:
        """Start a case without carrying unresolved evidence across samples."""
        self.case = identifier
        self.pending.clear()

    def fields(self, model: str) -> set[str]:
        """Select only explicitly requested discriminator observations."""
        return self._fields.get(model, set())

    def _credit(self, witnesses: list[str]) -> None:
        if self.case not in witnesses:
            self.budget.consume("states")
            self.budget.consume("bytes", len(self.case.encode("utf-8")))
            witnesses.append(self.case)

    def branch(
        self,
        binding: object,
        model: str,
        field: str,
        matches: tuple[bool | None, ...],
        *,
        empty: bool,
    ) -> None:
        """Retain actual branch matches in this exact resolved binding only."""
        if empty:
            return
        relevant = self._by_field.get((model, field), ())
        if not relevant:
            return
        self.budget.consume("work", len(relevant))
        unique = matches.count(True) == 1 and None not in matches
        for item in relevant:
            if unique and matches[item.branch] is True and item.discriminator is None:
                self._credit(self.branch_witnesses[item])
        if any(item.discriminator is not None for item in relevant):
            self.budget.consume("states")
            self.budget.consume("bytes", len(matches))
            self.pending.setdefault(id(binding), (binding, []))[1].append(
                (model, field, matches)
            )

    def scope(
        self,
        binding: object,
        states: Mapping[str, bool | None],
        terms: Mapping[str, tuple[TermKey, ...] | None],
    ) -> None:
        """Resolve explicit discriminators using the same binding's exact terms."""
        entry = self.pending.pop(id(binding), None)
        if entry is None:
            return
        for model, field, matches in entry[1]:
            candidates = []
            for item in self._by_field.get((model, field), ()):
                self.budget.consume("work")
                condition = item.discriminator
                if (
                    item.model != model
                    or item.field != field
                    or condition is None
                    or matches[item.branch] is not True
                ):
                    continue
                actual = terms.get(condition.field)
                if (
                    states.get(condition.field) is True
                    and actual is not None
                    and len(actual) == 1
                    and actual[0] == term_key(condition.equals)
                ):
                    candidates.append(item)
            if len({item.branch for item in candidates}) == 1:
                for item in candidates:
                    self._credit(self.branch_witnesses[item])

    def wants_scenarios(self, model: str, group: str) -> bool:
        """Retain ordinary first-match checking for unrequested scenario groups."""
        return (model, group) in self._scenarios

    def scenarios(
        self, model: str, group: str, matched: tuple[str, ...], *, unresolved: bool
    ) -> None:
        """Credit one unambiguous actual scenario; never every overlapping case."""
        if unresolved or len(matched) != 1:
            return
        for item in self._scenarios.get((model, group), ()):
            self.budget.consume("work")
            if (item.model, item.group, item.scenario) == (model, group, matched[0]):
                self._credit(self.scenario_witnesses[item])
