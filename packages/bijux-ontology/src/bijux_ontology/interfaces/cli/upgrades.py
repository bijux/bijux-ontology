"""Assess supplied versions using actual application and JSON-LD contracts."""

import argparse
from dataclasses import replace
from pathlib import Path

from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Location, Report, combine_reports
from bijux_ontology.interfaces.cli.arguments import vocabulary_input
from bijux_ontology.interfaces.cli.inputs import (
    UnsafeOutput,
    command_limits,
    input_paths,
    json_input,
    protect_inputs,
    source_map,
)
from bijux_ontology.interfaces.cli.results import CommandResult, display_data
from bijux_ontology.interfaces.cli.vocabularies import load_command_vocabulary
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.comparison import compare_context_usages
from bijux_ontology.jsonld.evidence.occurrences import ObservedUsage
from bijux_ontology.jsonld.processing.resources import OfflineDocumentLoader
from bijux_ontology.workflows.upgrades.assessment import assess_upgrade


def upgrade(options: argparse.Namespace) -> CommandResult:
    """Show actual affected uses; never infer global compatibility from no errors."""
    protect_inputs(options)
    limits = command_limits(options)
    rdf_selection = vocabulary_input(options)
    paths = input_paths(options)
    before = load_command_vocabulary(
        rdf_selection, limits, inputs=paths, output=options.output
    )
    changed = replace(
        rdf_selection,
        ontology=options.after,
        format=options.after_format
        if options.after_format is not None
        else rdf_selection.format,
        base=options.after_base
        if options.after_base is not None
        else rdf_selection.base,
        sources=options.after_sources
        if options.after_sources is not None
        else rdf_selection.sources,
    )
    after = load_command_vocabulary(
        changed, limits, inputs=paths, output=options.output
    )
    terms = (
        terms_from_data(
            json_input(options.inventory, limits), source=str(options.inventory)
        )
        if options.inventory is not None
        else ()
    )
    models = (
        models_from_data(
            json_input(options.declarations, limits), source=str(options.declarations)
        )
        if options.declarations is not None
        else ()
    )
    selected = (options.context_before, options.context_after, options.expectations)
    if any(item is not None for item in selected) and not all(
        item is not None for item in selected
    ):
        raise InputError(
            "Context upgrade requires --context-before, --context-after "
            "and --expectations"
        )
    if not any(item is not None for item in selected) and any(
        item is not None
        for item in (
            options.context_sources_before,
            options.context_sources_after,
            options.context_base_before,
            options.context_base_after,
        )
    ):
        raise InputError(
            "Context resources and bases require selected context documents"
        )
    context_reports = []
    observed: tuple[ObservedUsage, ...] = ()
    meaning = None
    if options.expectations is not None:
        expectations = expectations_from_data(
            json_input(options.expectations, limits), source=str(options.expectations)
        )
        views: list[PreparedUsages | None] = []
        reports = []
        for name, path, source_path, base in (
            (
                "before",
                options.context_before,
                options.context_sources_before,
                options.context_base_before,
            ),
            (
                "after",
                options.context_after,
                options.context_sources_after,
                options.context_base_after,
            ),
        ):
            try:
                resources = source_map(source_path, limits)
                protect_inputs(
                    options,
                    tuple(
                        value for value in resources.values() if isinstance(value, Path)
                    ),
                )
                prepared = prepare_usages(
                    json_input(path, limits),
                    base=base,
                    limits=limits,
                    document_loader=OfflineDocumentLoader(
                        resources, max_bytes=limits.max_bytes
                    ),
                )
                reports.append(prepared.check(expectations).with_source(str(path)))
                views.append(prepared)
            except UnsafeOutput:
                raise
            except (OSError, UnicodeError, InputError) as error:
                views.append(None)
                reports.append(
                    Report(
                        "context." + name,
                        requested=1,
                        issues=(
                            Issue(
                                "execution.limit_exceeded"
                                if isinstance(error, LimitExceeded)
                                else "input.invalid",
                                str(error),
                                str(path),
                                details=Location.from_error(error),
                                kind="exhausted"
                                if isinstance(error, LimitExceeded)
                                else "input_error",
                            ),
                        ),
                    )
                )
        context_reports.append((reports[0], reports[1]))
        old, new = views
        if old is not None:
            uses = []
            for expectation in expectations:
                try:
                    uses.append(old.observe(expectation.path))
                except InputError:
                    # The same required selector failure remains in old.check().
                    continue
            observed = tuple(uses)
        if old is not None and new is not None:
            meaning = compare_context_usages(
                old, new, tuple(item.path for item in expectations), limits=limits
            )
    assessment = assess_upgrade(
        before,
        after,
        terms=terms,
        models=models,
        observed_usages=observed,
        contract_reports=context_reports,
        local_namespaces=options.namespace,
        scopes=options.scope or ("local",),
        replacement_predicates=options.replacement_predicate,
        limits=limits,
    )
    report = (
        assessment.report
        if meaning is None
        else combine_reports("upgrade", assessment.report, meaning)
    )
    return CommandResult(
        report,
        display_data(
            {
                "contract_verdict": assessment.verdict,
                "usage_meaning": meaning.outcome if meaning is not None else None,
                "changes": assessment.changes.changes,
                "affected": assessment.affected,
                "contracts": assessment.contracts,
                "deprecated": assessment.deprecated,
            }
        ),
        output_safe=not any(issue.kind == "input_error" for issue in report.issues),
    )
