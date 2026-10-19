"""Bound and check independent application releases and actual owned outputs."""

from collections.abc import Mapping
from dataclasses import dataclass

from bijux_ontology.declarations.types import (
    ModelDeclaration,
)
from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.jsonld.paths import bound_index, bound_path
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.limits import Budget, IssueCollector
from bijux_ontology.models.checking import check_models
from bijux_ontology.models.emission.serialization import (
    InstanceCheck,
    check_instance,
    check_serialized,
)
from bijux_ontology.models.emission.types import ModelUsage
from bijux_ontology.models.resource_roles.checking import check_resource_roles
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.terms import bound_node, bound_text
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary
from bijux_ontology.workflows.upgrades.uses import UseIndex, index_uses

from .types import (
    ApplicationVersion,
)


@dataclass(frozen=True)
class _PreparedSample:
    """One actual checked document's comparison-only owned preparation."""

    view: PreparedUsages | None
    source: PreparedSource | None
    report: Report


@dataclass(frozen=True)
class _VersionCheck:
    """Raw completed version checks, retaining any prefix before exhaustion."""

    version: ApplicationVersion | None
    report: Report
    parts: Mapping[str, Report]
    samples: Mapping[str, InstanceCheck]
    prepared: Mapping[str, _PreparedSample]
    uses: UseIndex


def _unknown(scope: str, message: str, budget: Budget) -> Report:
    """Charge an explicit required unknown rather than manufacture agreement."""
    issues = IssueCollector(budget)
    issues.append(Issue("upgrade.application_unverified", message, kind="unverified"))
    return Report(scope, issues=tuple(issues), requested=1, complete=False)


def _stopped(report: Report) -> None:
    """Stop required tails immediately after a child exhausts shared work."""
    if any(issue.kind == "exhausted" for issue in report.issues):
        raise LimitExceeded("Application comparison stopped after exhausted child")


def _bound_usage(usage: ModelUsage, budget: Budget) -> None:
    """Requalify caller binding metadata against the operation's actual limits."""
    bound_text(usage.model, budget)
    bound_path(usage.path, budget)
    for value in (usage.reference, usage.graph):
        if isinstance(value, str):
            bound_text(value, budget)
    if usage.subject is not None and usage.subject.expected_iri is not None:
        bound_text(usage.subject.expected_iri, budget)


def _bound_model(model: ModelDeclaration, budget: Budget) -> None:
    """Bound diagnostic-bearing declarations before passing them to checkers."""
    for model_text in (model.identifier, model.location, *model.types):
        bound_text(model_text, budget)
    if model.subject.expected_iri is not None:
        bound_text(model.subject.expected_iri, budget)
    budget.consume("states", len(model.fields))
    for field in model.fields:
        for value in (field.name, field.key, field.location, field.predicate):
            if value is not None:
                bound_text(value, budget)
        budget.consume("states", len(field.values))
        for branch in field.values:
            for branch_text in (
                branch.kind,
                branch.datatype,
                branch.model,
                branch.container,
                branch.language,
                branch.direction,
                *branch.types,
            ):
                if isinstance(branch_text, str):
                    bound_text(branch_text, budget)
        if field.output is not None:
            for name in (
                "minimum_values",
                "maximum_values",
                "minimum_list_members",
                "maximum_list_members",
            ):
                value = getattr(field.output, name)
                if value is not None:
                    bound_index(value, budget)
    if model.coverage is not None:
        policy = model.coverage
        budget.consume(
            "states",
            len(policy.extensions)
            + len(policy.reverse_extensions)
            + len(policy.metadata)
            + len(policy.aliases),
        )
        for text in (*policy.extensions, *policy.reverse_extensions, *policy.metadata):
            bound_text(text, budget)
        for key, target in policy.aliases:
            bound_text(key, budget)
            bound_text(target, budget)
    budget.consume("states", len(model.emission_groups))
    for group in model.emission_groups:
        bound_text(group.identifier, budget)
        budget.consume("states", len(group.fields) + len(group.scenarios))
        for text in group.fields:
            bound_text(text, budget)
        for scenario in group.scenarios:
            bound_text(scenario.identifier, budget)
            budget.consume("states", len(scenario.present))
            for text in scenario.present:
                bound_text(text, budget)
            if scenario.condition is not None:
                bound_text(scenario.condition.field, budget)
                bound_node(scenario.condition.equals, budget)


def _preflight(version: ApplicationVersion, budget: Budget) -> None:
    """Qualify both immutable versions before invoking either caller callback."""
    for prefix, mapping in (
        ("term/", version.terms),
        ("model/", version.models),
        ("field/", version.fields),
        ("sample/", version.outputs),
        ("role/", version.role_uses),
    ):
        budget.consume("states", len(mapping))
        budget.consume("bytes", 64 * len(mapping))
        for name in mapping:
            bound_text(name, budget)
            bound_text(prefix + name, budget)
    for term in version.terms.values():
        for value in (term.iri, term.location, *term.kinds):
            bound_text(value, budget)
    for model in version.models.values():
        _bound_model(model, budget)
    for binding in version.fields.values():
        bound_text(binding.model, budget)
        bound_text(binding.field, budget)
        budget.consume("states", len(binding.samples))
        for sample, path in binding.samples.items():
            bound_text(sample, budget)
            bound_path(path, budget)
    budget.consume("resources", len(version.cases) + len(version.outputs))
    for case in version.cases:
        bound_text(case.identifier, budget)
        bound_text("sample/" + case.identifier, budget)
        bound_text(case._options_json, budget)
        for usage in case.usages:
            _bound_usage(usage, budget)
    for output in version.outputs.values():
        bound_text(output.document_json, budget)
        for usage in output.usages:
            _bound_usage(usage, budget)
    for url, value in version._resources_json.items():
        bound_text(url, budget)
        bound_text(value, budget)
    if version._context_json is not None:
        bound_text(version._context_json, budget)
    if version.base is not None:
        bound_text(version.base, budget)
    for use in version.role_uses.values():
        bound_text(use.location, budget)
        bound_text(use.term, budget)


def _prepared(
    result: InstanceCheck,
    version: ApplicationVersion,
    loader: OfflineDocumentLoader,
    budget: Budget,
) -> _PreparedSample:
    """Reuse actual output bytes, never call its serializer or checks again."""
    if result.document_json is None:
        return _PreparedSample(
            None,
            None,
            _unknown(
                "application.output", "No actual checked document is available", budget
            ),
        )
    view = None
    try:
        document = result.document
        view = prepare_usages(
            document, base=version.base, document_loader=loader, budget=budget
        )
        source = prepare_jsonld(
            document,
            base=version.base,
            document_loader=loader,
            direction=version.rdf_direction,
            budget=budget,
        )
        return _PreparedSample(view, source.source, source.report)
    except LimitExceeded:
        raise
    except InputError as error:
        issues = IssueCollector(budget)
        issues.append(Issue("input.invalid", str(error), kind="input_error"))
        return _PreparedSample(
            view,
            None,
            Report(
                "application.output", issues=tuple(issues), requested=1, complete=False
            ),
        )


def _check_version(
    version: ApplicationVersion | None,
    vocabulary: Vocabulary,
    name: str,
    budget: Budget,
) -> _VersionCheck:
    """Check this side's contracts against only this side's vocabulary/resources."""
    empty_uses = UseIndex((), Report("application.uses", allow_empty=True))
    if version is None:
        return _VersionCheck(
            None,
            _unknown(name, "No baseline application evidence was supplied", budget),
            {},
            {},
            {},
            empty_uses,
        )
    parts: dict[str, Report] = {}
    samples: dict[str, InstanceCheck] = {}
    prepared: dict[str, _PreparedSample] = {}
    reports: list[Report] = []
    issues = IssueCollector(budget)
    models = tuple(version.models.values())
    expected = (
        len(version.terms)
        + len(version.role_uses)
        + len(version.cases)
        + len(version.outputs)
        + bool(models)
    )
    checked = 0
    uses = empty_uses
    try:
        loader = OfflineDocumentLoader(version.resources, limits=budget.limits)
        for identifier, term in version.terms.items():
            report = check_vocabulary(vocabulary, (term,), budget=budget)
            parts["term/" + identifier] = report
            reports.append(report)
            checked += 1
            _stopped(report)
        for identifier, use in version.role_uses.items():
            report = check_resource_roles(vocabulary, (use,), budget=budget)
            parts["role/" + identifier] = report
            reports.append(report)
            checked += 1
            _stopped(report)
        cases = {str(case.identifier): case for case in version.cases}
        for identifier in (*cases, *version.outputs):
            budget.consume("work")
            if identifier in version.outputs:
                supplied = version.outputs[identifier]
                result = check_serialized(
                    supplied.document,
                    models,
                    supplied.usages,
                    vocabulary=vocabulary,
                    context=version.context,
                    base=version.base,
                    document_loader=loader,
                    rdf_direction=version.rdf_direction,
                    budget=budget,
                )
            else:
                sample = cases[identifier]
                result = check_instance(
                    sample.instance,
                    sample.serializer,
                    models,
                    sample.usages,
                    vocabulary=vocabulary,
                    options=sample.options,
                    context=version.context,
                    base=version.base,
                    document_loader=loader,
                    rdf_direction=version.rdf_direction,
                    budget=budget,
                )
            samples[identifier] = result
            parts["sample/" + identifier] = result.report
            reports.append(result.report)
            checked += 1
            _stopped(result.report)
            prepared[identifier] = _prepared(result, version, loader, budget)
            reports.append(prepared[identifier].report)
            _stopped(prepared[identifier].report)
        if models:
            declaration = next(
                (
                    child
                    for result in samples.values()
                    for child in result.report.children
                    if child.scope == "models"
                ),
                None,
            )
            if declaration is None:
                declaration = check_models(
                    models,
                    vocabulary,
                    rdf_direction=version.rdf_direction,
                    budget=budget,
                )
                reports.append(declaration)
            parts["models"] = declaration
            checked += 1
            _stopped(declaration)
        observed: list[ObservedUsage] = []
        for binding in version.fields.values():
            field = next(
                field
                for model in models
                if model.identifier == binding.model
                for field in model.fields
                if field.name == binding.field
            )
            roles = tuple(
                branch.role for branch in field.values if branch.role is not None
            )
            for case, path in binding.samples.items():
                budget.consume("work")
                item = prepared.get(case)
                if item is None or item.view is None:
                    continue
                try:
                    usage = item.view.observe(path)
                    observed.append(usage)
                    if roles:
                        depth = min(
                            (occurrence.depth for occurrence in usage.occurrences),
                            default=0,
                        )
                        for occurrence in usage.occurrences:
                            if (
                                occurrence.depth != depth
                                or occurrence.predicate != field.predicate
                                or occurrence.traits.kind == "literal"
                            ):
                                continue
                            endpoint = occurrence.traits.identifier
                            for use in version.role_uses.values():
                                budget.consume(
                                    "work", 1 + len(use.term) + len(endpoint or "")
                                )
                            if len(set(roles)) != 1 or not any(
                                str(use.term) == endpoint and use.contract in roles
                                for use in version.role_uses.values()
                            ):
                                issues.append(
                                    Issue(
                                        "upgrade.application_unverified",
                                        "Endpoint facet attribution is not established",
                                        kind="unverified",
                                    )
                                )
                                break
                except LimitExceeded:
                    raise
                except InputError:
                    issues.append(
                        Issue(
                            "upgrade.application_unverified",
                            "Selected field has no established source usage",
                            kind="unverified",
                        )
                    )
        uses = index_uses(
            tuple(version.terms.values()),
            models,
            role_uses=tuple(version.role_uses.values()),
            observed_usages=observed,
            budget=budget,
        )
        reports.append(uses.report)
        _stopped(uses.report)
    except LimitExceeded as error:
        issues.exhausted(str(error), location="application")
    return _VersionCheck(
        version,
        Report(
            name,
            checked,
            tuple(issues),
            requested=max(1, expected),
            children=tuple(reports),
        ),
        parts,
        samples,
        prepared,
        uses,
    )
