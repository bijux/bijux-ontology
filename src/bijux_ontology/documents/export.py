"""Verified maintained RDF serialization and explicit caller-owned output IO."""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from rdflib import Dataset, Graph

from bijux_ontology._json import decode_json
from bijux_ontology.jsonld.preparation import prepare_jsonld
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.snapshots import GraphSelection, PreparedSource, bind_namespaces

from ..errors import InputError, LimitExceeded, ResourceError
from ..reports import Issue, Report, combine_reports
from .preparation import prepare_source

_FORMATS = frozenset({"turtle", "nt", "xml", "trig", "nquads", "json-ld"})
_DATASET_FORMATS = frozenset({"trig", "nquads", "json-ld"})


@dataclass(frozen=True)
class RDFExport:
    """A verified representation or explicit loss, with no output written yet."""

    content: bytes | None
    report: Report
    format: str


def export_rdf(
    source: PreparedSource,
    *,
    format: str,
    graph_name: GraphSelection | None = None,
    union: bool = False,
    preserve_graphs: bool = True,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> RDFExport:
    """Serialize with RDFLib, then reparse and compare the explicit RDF scope.

    A dataset needs graph selection or explicit union for a graph-only format.
    Maintained serializers currently omit empty named graphs. Such exports fail
    preservation unless preserve_graphs=False explicitly excludes empty graphs.
    Unknown source empty-graph fidelity cannot become a verified full-dataset
    claim. Namespace prefixes aid readability; exact IRIs define identity.
    """
    if not isinstance(format, str) or format not in _FORMATS:
        raise InputError(f"Unsupported RDF export format: {format}")
    if type(union) is not bool or type(preserve_graphs) is not bool:
        raise InputError("RDF graph policies must be booleans")
    if graph_name is not None and union:
        raise InputError("Choose a graph name or union, not both")
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("Explicit limits conflict with the operation budget")
    operation = budget or Budget(limits)
    issues = IssueCollector(operation)
    selected: Graph
    exclusions: tuple[str, ...] = ()
    try:
        if graph_name is not None or union or format not in _DATASET_FORMATS:
            selected = source.graph(graph_name, union=union)
        else:
            selected = source.dataset() if source.is_dataset else source.graph()
        if isinstance(selected, Dataset):
            empty = [
                graph.identifier
                for graph in selected.graphs()
                if graph.identifier != selected.default_graph.identifier and not graph
            ]
            for _ in selected.graphs():
                operation.consume("work")
            if source.graph_fidelity != "complete" and preserve_graphs:
                issues.append(
                    Issue(
                        "rdf.export_graph_scope_unverified",
                        "Source empty-graph membership is not fully established",
                        kind="unverified",
                    )
                )
            if empty and preserve_graphs:
                issues.append(
                    Issue(
                        "rdf.export_empty_graph_loss",
                        "Maintained RDF serializers omit empty named graphs",
                        actual=f"{len(empty)} empty named graphs",
                    )
                )
            elif not preserve_graphs:
                for name in empty:
                    selected.remove_graph(name)
                exclusions = ("empty-named-graphs",)
                issues.append(
                    Issue(
                        "rdf.export_empty_graph_exclusion",
                        "The caller explicitly excluded empty named-graph preservation",
                        severity="info",
                    )
                )
        if any(issue.kind != "finding" or issue.severity != "info" for issue in issues):
            return RDFExport(
                None,
                Report(
                    "rdf.export",
                    0,
                    tuple(issues),
                    requested=1,
                    complete=False,
                    regime="rdf-serialization",
                    families=("serialization",),
                ),
                format,
            )
        for _ in selected:
            operation.consume("work")
        serializable = selected
        if format in _DATASET_FORMATS and not isinstance(selected, Dataset):
            serializable = Dataset()
            for triple in selected:
                serializable.default_graph.add(triple)
            bind_namespaces(serializable, selected)
        try:
            content = serializable.serialize(format=format, encoding="utf-8")
        except ValueError as error:
            issues.append(
                Issue(
                    "rdf.export_representation_unverified",
                    str(error),
                    kind="unverified",
                )
            )
            return RDFExport(
                None,
                Report(
                    "rdf.export",
                    0,
                    tuple(issues),
                    requested=1,
                    complete=False,
                    regime="rdf-serialization",
                    families=("serialization",),
                    exclusions=exclusions,
                ),
                format,
            )
        if len(content) > operation.limits.max_bytes:
            raise LimitExceeded("RDF export exceeds max_bytes")
        operation.consume("bytes", len(content))
        if format == "json-ld":
            prepared = prepare_jsonld(decode_json(content), budget=operation)
            if prepared.source is None:
                return RDFExport(None, prepared.report, format)
            reparsed = prepared.source
        else:
            reparsed = prepare_source(content, format=format, budget=operation)
        comparison = compare_rdf(
            selected,
            reparsed,
            budget=operation,
            timeout_seconds=timeout_seconds,
            # The preflight above either establishes no selected empty graphs or
            # records their explicit exclusion. The TriG parser independently
            # exposes only nonempty graphs; it cannot establish their absence.
            ignore_empty_graphs=format == "trig",
        )
        scope = Report(
            "rdf.export_scope",
            1,
            tuple(issues),
            regime="rdf-serialization",
            families=("serialization",),
            exclusions=exclusions,
        )
        report = replace(
            combine_reports("rdf.export", scope, comparison),
            regime="rdf-serialization",
        )
        if report.exit_code() != 0 or any(
            issue.kind == "unverified" for issue in report.issues
        ):
            return RDFExport(None, report, format)
        return RDFExport(content, report, format)
    except LimitExceeded as error:
        issues.exhausted(str(error))
        return RDFExport(
            None,
            Report(
                "rdf.export",
                0,
                tuple(issues),
                requested=1,
                complete=False,
                regime="rdf-serialization",
                families=("serialization",),
                exclusions=exclusions,
            ),
            format,
        )


def write_export(
    exported: RDFExport,
    destination: Path | BinaryIO,
    *,
    overwrite: bool = False,
) -> None:
    """Write accepted bytes to an explicit destination; keep caller streams open.

    Existing paths are protected unless overwrite=True explicitly opts in. This
    function never chooses an output path or replaces any source automatically.
    """
    if exported.content is None or exported.report.exit_code() != 0:
        raise InputError("Only an accepted RDF export can be written")
    if type(overwrite) is not bool:
        raise InputError("overwrite must be a boolean")
    if not isinstance(destination, Path):
        if not callable(getattr(destination, "write", None)):
            raise InputError("Export destination must be a path or binary writer")
        if getattr(destination, "closed", False):
            raise ResourceError("Cannot write to a closed export stream")
    try:
        if isinstance(destination, Path):
            with destination.open("wb" if overwrite else "xb") as stream:
                written = stream.write(exported.content)
        else:
            written = destination.write(exported.content)
        if written != len(exported.content):
            raise ResourceError("The output stream did not accept the complete export")
    except OSError as error:
        raise ResourceError(f"Cannot write RDF export: {error}") from error
