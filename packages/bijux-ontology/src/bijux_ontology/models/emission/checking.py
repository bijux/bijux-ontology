"""Orchestrate owned JSON-LD preparation and representative output checks."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, cast

from rdflib import Graph, Literal

from bijux_ontology._json import own_json
from bijux_ontology.declarations.types import ModelDeclaration
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.paths import bound_path, location
from bijux_ontology.jsonld.resources import DocumentLoader
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.models.emission.application import retain_enumeration_report
from bijux_ontology.models.emission.direction import DirectionCheck
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.registry import index_models
from bijux_ontology.models.resource_roles.checking import _RoleChecks
from bijux_ontology.rdf.datatype_evaluators.registry import DatatypeRegistry
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.sources.view import Vocabulary

from .context import EmissionContext
from .coverage import check_coverage
from .groups import check_output_groups
from .source import check_sources

if TYPE_CHECKING:
    from bijux_ontology.models.corpus.types import _CorpusEvidence


def check_emission(
    document: object,
    models: Sequence[ModelDeclaration],
    usages: Sequence[ModelUsage],
    *,
    vocabulary: Vocabulary | Graph | VocabularyView | None = None,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
    budget: Budget | None = None,
    rdf_direction: DirectionPolicy | None = None,
    _corpus: _CorpusEvidence | None = None,
    datatype_registry: DatatypeRegistry | None = None,
    context_expectations: Sequence[ContextExpectation] = (),
    _prepared: PreparedUsages | None = None,
) -> Report:
    """Check selected actual outputs, without claiming all hypothetical instances.

    Each occurrence must match one alternative branch. Nested model nodes need
    established source or graph-qualified entity bindings. Supported unambiguous
    children are discovered automatically; explicit bindings can constrain them.
    Unsupplied descriptions
    stay unknown unless their reference binding explicitly requires closed coverage.
    Predicates, reversal, collection membership and literal traits come from the
    shared standards-processed usage snapshot, never a second expansion engine.
    Type checks compare explicit emitted types, without ontology entailment.
    Additional context expectations check caller-selected source meanings in
    that same preparation, independently of source or joined-entity bindings.
    """
    if datatype_registry is not None and (
        not isinstance(datatype_registry, DatatypeRegistry)
    ):
        raise InputError("Expected an explicit DatatypeRegistry")
    if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
        raise InputError("Unknown RDF direction policy")
    if budget is not None and limits is not None and (budget.limits != limits):
        raise InputError("Explicit limits conflict with the shared operation budget")
    selected = limits or (budget.limits if budget is not None else Limits())
    budget = budget if budget is not None else Budget(selected)
    issues = IssueCollector(budget)
    enumeration_reports: dict[tuple[int, bool], Report] = {}
    checked = 0
    complete = True
    native_literals: dict[str, Literal] = {}
    direction_check = DirectionCheck(rdf_direction)
    role_checks: _RoleChecks | None = None
    state: EmissionContext | None = None
    try:
        if _prepared is not None:
            if (
                not isinstance(_prepared, PreparedUsages)
                or _prepared._budget is not budget
            ):
                raise InputError("Prepared usages require their originating Budget")
            if document is not _prepared._document or base != _prepared._base:
                raise InputError("Prepared usage document or base differs")
            if document_loader is not _prepared._loader:
                raise InputError("Prepared usages require their captured context owner")
            if (
                _prepared.allowed_drops != allowed_drops
                or _prepared.allowed_omissions
                or _prepared._omission_issues
            ):
                raise InputError("Prepared usages carry unrelated exclusions")
        if not isinstance(context_expectations, Sequence) or isinstance(
            cast(object, context_expectations), str | bytes
        ):
            raise InputError("Context expectations require a finite sequence")
        count = len(context_expectations)
        budget.consume("states", count)
        budget.consume("bytes", 16 * count)
        supplied_expectations = []
        for index in range(count):
            budget.consume("work")
            try:
                expectation = context_expectations[index]
            except IndexError as error:
                raise InputError("Context expectation inventory changed") from error
            if not isinstance(expectation, ContextExpectation):
                raise InputError("Expected typed ContextExpectation records")
            bound_path(expectation.path, budget)
            supplied_expectations.append(expectation)
        registry = index_models(models, budget, issues)
        role_checks = None
        for declaration in registry.values():
            for field in declaration.fields:
                for index, branch in enumerate(field.values):
                    budget.consume("work")
                    if (
                        branch.role is not None
                        and vocabulary is not None
                        and (role_checks is None)
                    ):
                        role_checks = _RoleChecks(vocabulary, budget)
                    retain_enumeration_report(
                        branch.constraints,
                        enumeration_reports,
                        budget=budget,
                        issues=issues,
                    )
                    if branch.model is not None and branch.model not in registry:
                        issues.append(
                            Issue(
                                "input.model_reference_missing",
                                "Value branch references an unknown model",
                                f"{declaration.identifier}.{field.name}[{index}]",
                                kind="input_error",
                                actual=branch.model,
                            )
                        )
        snapshot = own_json(
            document,
            max_bytes=selected.max_bytes,
            max_depth=selected.max_depth,
            budget=budget,
        )
        prepared = (
            _prepared
            if _prepared is not None
            else prepare_usages(
                snapshot,
                base=base,
                document_loader=document_loader,
                allowed_drops=allowed_drops,
                limits=selected,
                budget=budget,
            )
        )
        state = EmissionContext(
            snapshot,
            prepared,
            registry,
            list(usages),
            budget,
            issues,
            native_literals,
            direction_check,
            role_checks,
            rdf_direction,
            datatype_registry,
            _corpus,
        )
        for binding in state.bindings:
            budget.consume("work")
            if not isinstance(binding, ModelUsage):
                raise InputError("Expected typed model usage bindings")
            if binding.model not in registry:
                issues.append(
                    Issue(
                        "input.model_reference_missing",
                        "Usage references an unknown model",
                        location(binding.path),
                        kind="input_error",
                        actual=binding.model,
                    )
                )
        expectations = check_sources(state)
        budget.consume("states", len(supplied_expectations))
        expectations.extend(supplied_expectations)
        check_coverage(state)
        check_output_groups(state)
        mapping = prepared.check(expectations, allow_empty=True)
        checked += mapping.checked
        issues.extend(mapping.issues)
        complete = mapping.complete
        preservation = direction_check.check(
            prepared._document, base=base, loader=prepared._loader, budget=budget
        )
        if preservation is not None:
            checked += preservation.checked
            list.extend(issues, preservation.issues)
            complete = complete and preservation.complete
    except LimitExceeded as error:
        complete = False
        issues.exhausted(str(error))
    checked += state.checked if state is not None else 0
    return Report(
        "model-emission",
        checked,
        tuple(issues),
        children=(
            *enumeration_reports.values(),
            *(
                (role_checks.exhaustion,)
                if role_checks is not None and role_checks.exhaustion is not None
                else ()
            ),
        ),
        complete=complete,
        families=("representative-serialization", "literal-values"),
        requested=max(checked, len(usages)),
    )
