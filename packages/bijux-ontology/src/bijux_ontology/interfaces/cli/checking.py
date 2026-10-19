"""Console selections delegate to the same supported Python operations."""

import argparse
from collections.abc import Callable
from pathlib import Path
from tempfile import mkdtemp
from typing import cast

from bijux_ontology._json import object_value
from bijux_ontology.declarations.codecs import (
    models_from_data,
    terms_from_data,
)
from bijux_ontology.errors import InputError
from bijux_ontology.interfaces.cli.arguments import vocabulary_input
from bijux_ontology.interfaces.cli.inputs import (
    command_limits,
    input_paths,
    json_input,
    protect_inputs,
    source_map,
)
from bijux_ontology.interfaces.cli.results import CommandResult, display_data
from bijux_ontology.interfaces.cli.vocabularies import load_command_vocabulary
from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.codecs import expectations_from_data
from bijux_ontology.jsonld.resources import OfflineDocumentLoader
from bijux_ontology.limits import Limits
from bijux_ontology.models.checking import check_models
from bijux_ontology.rdf.literals import Regime as ModelRegime
from bijux_ontology.reports import Report
from bijux_ontology.vocabulary.builtins import Regime
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.inspection import inspect_vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology
from bijux_ontology.workflows.projects.bundles import load_bundle
from bijux_ontology.workflows.projects.checking import PreparedChecks, check_project
from bijux_ontology.workflows.projects.configuration import ProjectConfig, load_project


def project(options: argparse.Namespace) -> ProjectConfig:
    """Apply only explicit command overrides to a configuration-owned project."""
    if getattr(options, "bundle", False):
        bundle = load_bundle(options.configuration, limits=command_limits(options))
        if options.configuration.is_dir():
            protect_inputs(
                options,
                tuple(options.configuration / name for name in bundle.resource_paths),
            )
        root = Path.cwd() / "artifacts" / "ontology-bundles"
        root.mkdir(parents=True, exist_ok=True)
        destination = Path(mkdtemp(prefix="resources-", dir=root)) / "resources"
        config = bundle.materialize(destination)
    else:
        config = load_project(options.configuration)
    data = config.to_dict()
    overrides = {
        key: value
        for key in Limits.__dataclass_fields__
        if (value := getattr(options, key, None)) is not None
    }
    if overrides:
        configured = object_value(data.get("limits", {}), location="limits")
        data["limits"] = configured | overrides
    if options.strict:
        policy = object_value(data.get("policy", {}), location="policy")
        data["policy"] = policy | {"strict": True}
    if options.minimum_severity is not None or options.permit:
        policy = object_value(data.get("policy", {}), location="policy")
        if options.minimum_severity is not None:
            policy["minimum_severity"] = options.minimum_severity
        if options.permit:
            existing = policy.get("exceptions", [])
            assert isinstance(existing, list)
            policy["exceptions"] = existing + [
                {"code": code, "location": location}
                for code, location in options.permit
            ]
        data["policy"] = policy
    config = ProjectConfig(data, directory=config.directory, source=config.source)
    paths = [cast(Path, source.content) for source in config.rdf.values()]
    paths.extend(config.contexts.values())
    for check in config.checks():
        members = check.get("documents", [])
        if not isinstance(members, list):
            raise InputError("Context batch documents must be an array")
        jobs = [
            check,
            *(object_value(member, location="documents") for member in members),
        ]
        for job in jobs:
            for key in ("inventory", "declarations", "document", "expectations"):
                if check["kind"] in ("vocabulary", "models", "context") and key in job:
                    paths.append(config.path(job[key]))
    protect_inputs(options, tuple(paths))
    return config


def run_check(
    options: argparse.Namespace,
    *,
    completed: Callable[[Report], None] | None = None,
    configuration: ProjectConfig | None = None,
) -> Report:
    """Run selected checks; programmer failures retain their ordinary tracebacks."""
    protect_inputs(options)
    limits = command_limits(options)
    if options.action == "project":
        return check_project(
            PreparedChecks(configuration or project(options)), completed=completed
        )
    if options.action == "context":
        resources = source_map(options.sources, limits)
        protect_inputs(
            options,
            tuple(value for value in resources.values() if isinstance(value, Path)),
        )
        return check_context(
            json_input(options.document, limits),
            expectations_from_data(
                json_input(options.expectations, limits),
                source=str(options.expectations),
            ),
            base=options.base,
            document_loader=OfflineDocumentLoader(
                resources, max_bytes=limits.max_bytes
            ),
            allowed_drops=frozenset(options.allow_drop),
            limits=limits,
        ).with_source(str(options.document))
    vocabulary = load_command_vocabulary(
        vocabulary_input(options),
        limits,
        inputs=input_paths(options),
        output=options.output,
    )
    if options.action == "skos":
        from rdflib.namespace import SKOS

        from bijux_ontology.skos.checking import check_skos

        return check_skos(
            vocabulary,
            scopes=options.scope or ("local", "imports"),
            rules=options.rule,
            profile=options.profile,
            hierarchy_basis=options.hierarchy_basis,
            concepts=options.concept,
            schemes=options.scheme,
            languages=options.language,
            language_fallback=options.language_fallback,
            definition_predicates=options.definition_predicate
            or (str(SKOS.definition),),
            closed_targets=options.closed_targets,
            notation_collections=options.notation_collection,
            replacement_predicates=options.replacement_predicate,
            limits=limits,
        )
    if options.action == "models":
        return check_models(
            models_from_data(
                json_input(options.declarations, limits),
                source=str(options.declarations),
            ),
            vocabulary,
            regime=cast(ModelRegime, options.regime),
            limits=limits,
        )
    if options.action == "ontology":
        return check_ontology(
            vocabulary,
            regime=cast(Regime, options.regime),
            families=options.family
            if options.family is not None
            else (
                "references",
                "declarations",
                "headers",
                "expressions",
                "annotations",
                "reserved",
            ),
            include_imports=not options.local_only,
            include_lookup=options.include_lookup,
            required_annotations=options.require_annotation,
            languages=options.language,
            used_terms=options.used_term,
            replacement_predicates=options.replacement_predicate,
            limits=limits,
        )
    return check_vocabulary(
        vocabulary,
        terms_from_data(
            json_input(options.inventory, limits), source=str(options.inventory)
        ),
        local_namespaces=options.namespace,
        exclude=options.exclude,
        require_coverage=options.coverage,
        coverage_terms=options.coverage_term,
        coverage_kinds=options.coverage_kind,
        coverage_scope=options.coverage_scope,
        regime=cast(Regime, options.regime),
        check_references=options.references,
        lint=options.lint,
        allow_empty=options.allow_empty,
        limits=limits,
    )


def run_command(
    options: argparse.Namespace,
    *,
    completed: Callable[[Report], None] | None = None,
    configuration: ProjectConfig | None = None,
) -> CommandResult:
    """Dispatch presentation-rich inspection without a second semantic engine."""
    if options.action == "explain":
        from bijux_ontology.interfaces.cli.diagnostics import explain_code

        explanation = explain_code(options.code)
        if explanation is None:
            raise InputError("Unknown diagnostic code", location=options.code)
        return CommandResult(
            Report("diagnostic.explanation", 1, families=("diagnostic-help",)),
            explanation.to_dict(),
        )
    if options.action == "transform":
        from bijux_ontology.interfaces.cli.transforms import transform

        return transform(options)
    if options.action == "upgrade":
        from bijux_ontology.interfaces.cli.upgrades import upgrade

        return upgrade(options)
    if options.action != "inspect":
        return CommandResult(
            run_check(options, completed=completed, configuration=configuration)
        )

    limits = command_limits(options)
    view = inspect_vocabulary(
        load_command_vocabulary(
            vocabulary_input(options),
            limits,
            inputs=input_paths(options),
            output=options.output,
        ),
        scopes=options.scope or ("local", "imports"),
        limits=limits,
    )
    if options.term is not None:
        described = view.describe(options.term)
        return CommandResult(described.report, display_data(described))
    if options.search is not None:
        selected = view.search(options.search, languages=tuple(options.language))
        return CommandResult(selected.report, display_data(selected.items))
    summary = view.summary()
    return CommandResult(
        Report("terms.summary", 1, families=("asserted-summary",)),
        display_data(summary),
    )
