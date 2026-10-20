"""Explicit new RDF artifacts delegate to copied transformation and export APIs."""

import argparse
from pathlib import Path
from typing import cast

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report, combine_reports
from bijux_ontology.documents.export import export_rdf, write_export
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget
from bijux_ontology.interfaces.cli.inputs import (
    command_limits,
    input_paths,
    json_input,
    protect_inputs,
    protect_paths,
    rdf_input,
)
from bijux_ontology.interfaces.cli.results import CommandResult
from bijux_ontology.json.values import object_value
from bijux_ontology.rdf.sources.snapshots import PreparedSource
from bijux_ontology.rdf.transforms import (
    extract_neighbourhood,
    merge_sources,
    recheck_transformation,
    remap_iris,
)
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.workflows.ontology.checking import check_ontology


def transform(options: argparse.Namespace) -> CommandResult:
    """Create a new explicit candidate only after its requested checks succeed."""
    protect_inputs(options)
    protect_paths(
        input_paths(options, exclude=frozenset({"destination"})),
        options.destination,
    )
    if options.sources is not None or options.lookup:
        raise InputError(
            "Transformations retain import assertions without fetching; "
            "omit lookups and sources"
        )
    operation = options.operation
    if options.mapping is not None and operation != "remap":
        raise InputError("Only remap accepts --mapping")
    if (
        options.term or options.predicate or options.without_blank_closure
    ) and operation != "extract":
        raise InputError("Only extract accepts seed terms and closure options")
    if options.merge_source and operation != "merge":
        raise InputError("Only merge accepts --merge-source")
    limits = command_limits(options)
    budget = Budget(limits)

    def source(path: Path) -> PreparedSource:
        """Prepare each supplied snapshot under its explicit selected graph scope."""
        prepared = prepare_source(
            rdf_input(path, format=options.format, base=options.base, limits=limits),
            budget=budget,
        )
        if options.graph is not None or options.union:
            return prepare_source(
                prepared.graph(options.graph, union=options.union), budget=budget
            )
        return prepared

    original = source(options.ontology)
    candidate: PreparedSource | None = original
    reports = []
    if operation == "extract":
        result = extract_neighbourhood(
            original,
            options.term,
            predicates=options.predicate,
            include_blank_nodes=not options.without_blank_closure,
            budget=budget,
        )
        reports.append(result.report)
        candidate = result.source
    elif operation == "remap":
        if options.mapping is None:
            raise InputError("remap requires --mapping")
        mapping = object_value(
            json_input(options.mapping, limits), location=str(options.mapping)
        )
        if any(not isinstance(value, str) for value in mapping.values()):
            raise InputError("IRI replacement values must be strings")
        result = remap_iris(original, cast(dict[str, str], mapping), budget=budget)
        reports.append(result.report)
        candidate = result.source
    elif operation == "merge":
        if not options.merge_source:
            raise InputError("merge requires at least one --merge-source")
        result = merge_sources(
            [original, *(source(path) for path in options.merge_source)],
            union=options.union,
            budget=budget,
        )
        reports.append(result.report)
        candidate = result.source
    if candidate is None:
        return CommandResult(combine_reports("rdf.candidate", *reports))
    if options.recheck_ontology:

        def check(prepared: PreparedSource) -> Report:
            """The caller explicitly requested asserted structural checking."""
            return check_ontology(
                load_vocabulary(prepared.graph(), limits=limits), limits=limits
            )

        reports.append(
            recheck_transformation(
                original, candidate, {"ontology": check}, budget=budget
            )
        )
    exported = export_rdf(
        candidate,
        format=options.to_format,
        preserve_graphs=not options.ignore_empty_graphs,
        budget=budget,
    )
    reports.append(exported.report)
    report = combine_reports("rdf.candidate", *reports)
    written = False
    if report.exit_code() == 0 and not any(
        issue.kind == "unverified" for issue in report.issues
    ):
        write_export(exported, options.destination)
        written = True
    return CommandResult(
        report,
        {
            "destination": str(options.destination),
            "written": written,
            "operation": operation,
            "format": options.to_format,
        },
    )
