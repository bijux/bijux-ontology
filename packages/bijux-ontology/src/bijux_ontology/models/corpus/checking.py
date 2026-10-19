"""Required alternative/scenario coverage from actual bounded serializer cases."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TypeVar

from rdflib import Graph

from bijux_ontology._json import own_json
from bijux_ontology.absence import ABSENT, Absent
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.models.contracts.registry import index_models
from bijux_ontology.models.emission.serialization import InstanceCheck, check_instance
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .types import (
    BranchCoverage,
    ScenarioCoverage,
    _CorpusEvidence,
)


@dataclass(frozen=True, init=False)
class SerializerCase:
    """One actual caller-owned serialization with owned explicit options/usages."""

    identifier: str
    instance: object
    serializer: Callable[..., object]
    usages: tuple[ModelUsage, ...]
    _options_json: str = dataclass_field(repr=False)

    def __init__(
        self,
        identifier: str,
        instance: object,
        serializer: Callable[..., object],
        usages: Sequence[ModelUsage],
        *,
        options: Mapping[str, object] | None = None,
    ) -> None:
        if not isinstance(identifier, str) or not identifier.strip():
            raise InputError("Serializer case requires a nonempty identifier")
        if not callable(serializer):
            raise InputError("Serializer case requires a callable")
        _require_sequence(usages)
        if len(usages) > Limits().max_states:
            raise LimitExceeded("Serializer usages exceed the finite ownership bound")
        owned_usages = []
        for index in range(len(usages)):
            usage = usages[index]
            if not isinstance(usage, ModelUsage):
                raise InputError("Serializer case requires typed ModelUsage bindings")
            owned_usages.append(usage)
        owned = own_json(dict(options) if options is not None else {})
        object.__setattr__(self, "identifier", identifier)
        object.__setattr__(self, "instance", instance)
        object.__setattr__(self, "serializer", serializer)
        object.__setattr__(self, "usages", tuple(owned_usages))
        object.__setattr__(self, "_options_json", json.dumps(owned, allow_nan=False))

    @property
    def options(self) -> dict[str, object]:
        """Export independent options without exposing the stored snapshot."""
        result: dict[str, object] = json.loads(self._options_json)
        return result


@dataclass(frozen=True)
class CorpusCheck:
    """Actual case results, finite coverage witnesses, and their combined report.

    Witnesses describe exercised supplied samples, never universal correctness.
    A covered branch can still have a failing sample retained in ``report``.
    """

    cases: tuple[tuple[str, InstanceCheck], ...]
    branches: tuple[tuple[BranchCoverage, tuple[str, ...]], ...]
    scenarios: tuple[tuple[ScenarioCoverage, tuple[str, ...]], ...]
    coverage: Report
    report: Report


T = TypeVar("T")


def _require_sequence(items: object) -> None:
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise InputError("Corpus inventories require finite sequences")


def _capture(items: Sequence[T], budget: Budget, cap: int) -> tuple[T, ...]:
    _require_sequence(items)
    count = len(items)
    if count > cap:
        raise LimitExceeded("Corpus inventory exceeds selected finite bound")
    result = []
    for index in range(count):
        budget.consume("work")
        budget.consume("states")
        result.append(items[index])
    return tuple(result)


def _validate(
    branches: tuple[BranchCoverage, ...],
    scenarios: tuple[ScenarioCoverage, ...],
    registry: dict[str, ModelDeclaration],
    budget: Budget,
) -> None:
    for item in branches:
        budget.consume("work")
        if not isinstance(item, BranchCoverage) or item.model not in registry:
            raise InputError("Branch coverage requires an unambiguous declared model")
        budget.consume("bytes", len(str(item).encode("utf-8")))
        budget.consume("work", len(registry[item.model].fields))
        fields = {field.name: field for field in registry[item.model].fields}
        if item.field not in fields or item.branch >= len(fields[item.field].values):
            raise InputError("Branch coverage references an unknown field alternative")
        if item.discriminator is not None:
            condition = fields.get(item.discriminator.field)
            if condition is None or condition.predicate == "@id":
                raise InputError("Branch discriminator requires a declared RDF field")
            if any(
                field.name != condition.name
                and (field.predicate, field.reverse)
                == (condition.predicate, condition.reverse)
                for field in fields.values()
            ):
                raise InputError(
                    "Discriminator aliases require one explicit RDF field contract"
                )
    for scenario in scenarios:
        budget.consume("work")
        if not isinstance(scenario, ScenarioCoverage) or scenario.model not in registry:
            raise InputError("Scenario coverage requires an unambiguous declared model")
        budget.consume("bytes", len(str(scenario).encode("utf-8")))
        budget.consume(
            "work",
            sum(
                1 + len(group.scenarios)
                for group in registry[scenario.model].emission_groups
            ),
        )
        if not any(
            group.identifier == scenario.group
            and any(case.identifier == scenario.scenario for case in group.scenarios)
            for group in registry[scenario.model].emission_groups
        ):
            raise InputError(
                "Scenario coverage references an unknown declared scenario"
            )


def check_serializer_corpus(
    cases: Sequence[SerializerCase],
    models: Sequence[ModelDeclaration],
    *,
    vocabulary: Vocabulary | Graph,
    required_branches: Sequence[BranchCoverage] = (),
    required_scenarios: Sequence[ScenarioCoverage] = (),
    context: object | Absent = ABSENT,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    max_cases: int = 128,
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
) -> CorpusCheck:
    """Check actual calls once each and account for explicitly required witnesses.

    All semantic work and retained coverage share one operation budget. Ordinary
    caller callbacks execute in process with caller-owned lifetimes. Coverage is
    representative only; each case's correctness remains independently visible.
    """
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unsupported RDF direction policy")
    if type(max_cases) is not int or max_cases <= 0:
        raise InputError("max_cases requires a positive integer")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Explicit limits conflict with the shared operation budget")
    for inventory in (cases, models, required_branches, required_scenarios):
        _require_sequence(inventory)
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    results: list[tuple[str, InstanceCheck]] = []
    branches: tuple[BranchCoverage, ...] = ()
    scenarios: tuple[ScenarioCoverage, ...] = ()
    evidence = _CorpusEvidence((), (), operation)
    complete = True
    pending_cases = len(cases)
    required_count = len(required_branches) + len(required_scenarios)
    try:
        branches = _capture(required_branches, operation, operation.limits.max_states)
        scenarios = _capture(required_scenarios, operation, operation.limits.max_states)
        if not branches and not scenarios:
            raise InputError(
                "Corpus coverage requires a nonempty explicit requirement inventory"
            )
        declarations = _capture(models, operation, operation.limits.max_states)
        registry = index_models(declarations, operation, issues)
        _validate(branches, scenarios, registry, operation)
        if len(set(branches)) != len(branches) or len(set(scenarios)) != len(scenarios):
            raise InputError("Corpus coverage requirements must be unique")
        evidence = _CorpusEvidence(branches, scenarios, operation)
        captured = _capture(
            cases, operation, min(max_cases, operation.limits.max_resources)
        )
        if any(not isinstance(case, SerializerCase) for case in captured):
            raise InputError("Expected typed serializer cases")
        if len({case.identifier for case in captured}) != len(captured):
            raise InputError("Serializer case identifiers must be unique")
        for case in captured:
            operation.consume("resources")
            operation.consume("bytes", len(case.identifier.encode("utf-8")))
            evidence.begin(case.identifier)
            result = check_instance(
                case.instance,
                case.serializer,
                declarations,
                case.usages,
                vocabulary=vocabulary,
                options=case.options,
                context=context,
                base=base,
                document_loader=document_loader,
                budget=operation,
                _corpus=evidence,
                rdf_direction=rdf_direction,
            )
            results.append((case.identifier, result))
            pending_cases -= 1
            if result.report.outcome == "exhausted":
                raise LimitExceeded("Corpus semantic budget exhausted")
    except LimitExceeded as error:
        complete = False
        issues.exhausted(
            f"{error}; {pending_cases} required serializer cases remain",
            location="serialization.corpus.remaining",
        )
    covered = 0
    requirements: tuple[BranchCoverage | ScenarioCoverage, ...] = (
        *branches,
        *scenarios,
    )
    for requirement in requirements:
        witnesses = (
            evidence.branch_witnesses.get(requirement, [])
            if isinstance(requirement, BranchCoverage)
            else evidence.scenario_witnesses.get(requirement, [])
        )
        if witnesses:
            covered += 1
        elif complete:
            try:
                issues.append(
                    Issue(
                        "serialization.coverage_missing",
                        "No unambiguous actual sample witnesses this required "
                        "alternative or scenario",
                        str(requirement),
                        kind="unverified",
                    )
                )
            except LimitExceeded as error:
                complete = False
                issues.exhausted(str(error), location="serialization.corpus.remaining")
    coverage = Report(
        "serialization.corpus.coverage",
        covered,
        tuple(issues),
        requested=max(1, required_count),
        complete=complete and not pending_cases,
        families=("representative-serialization",),
    )
    case_reports = tuple(
        Report(
            f"serialization.case[{name}]",
            children=(result.report,),
            checked=result.report.checked,
            requested=result.report.requested,
        )
        for name, result in results
    )
    report = combine_reports("serialization.corpus", coverage, *case_reports)
    return CorpusCheck(
        tuple(results),
        tuple(
            (item, tuple(evidence.branch_witnesses.get(item, ()))) for item in branches
        ),
        tuple(
            (item, tuple(evidence.scenario_witnesses.get(item, ())))
            for item in scenarios
        ),
        coverage,
        report,
    )
