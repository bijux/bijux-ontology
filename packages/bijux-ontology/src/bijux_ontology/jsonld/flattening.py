"""Owned JSON-LD flattening, exact RDF preservation and qualified node bindings."""

import json
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import cast

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report, combine_reports
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.paths import SourcePath, location
from bijux_ontology.json.values import own_json
from bijux_ontology.rdf.comparison import compare_rdf

from .conversion import DirectionPolicy
from .expansion import _drop_issues, expand_document
from .preparation import prepare_jsonld
from .resources import DocumentLoader, _consume_work, _json_size, context_resources
from .runtime import processor


def _json(value: object) -> str:
    """Retain immutable JSON while exports remain independent caller copies."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class FlatNode:
    """One flattened description and its graph-qualified output binding.

    expanded_paths address the expanded input, never the original compact JSON.
    They are present only when separately requested correspondence is established.
    Generated blank labels identify nodes only within this flattening result.
    """

    identifier: str
    graph: str | None
    path: SourcePath
    value_json: str
    expanded_paths: tuple[SourcePath, ...] = ()

    @property
    def value(self) -> dict[str, object]:
        """Return an independent copy of this flat node description."""
        return cast(dict[str, object], json.loads(self.value_json))


@dataclass(frozen=True)
class FlattenedDocument:
    """Checked flat output; preservation and node correspondence are separate."""

    document_json: str | None
    nodes: tuple[FlatNode, ...]
    report: Report
    correspondence_report: Report | None = None

    @property
    def document(self) -> list[dict[str, object]] | None:
        """Return owned flat JSON without exposing mutable cached state."""
        return (
            cast(list[dict[str, object]], json.loads(self.document_json))
            if self.document_json is not None
            else None
        )

    def bind(self, identifier: str, *, graph: str | None = None) -> FlatNode:
        """Bind one exact output ID in the default or explicitly named graph."""
        matches = [
            node
            for node in self.nodes
            if node.identifier == identifier and node.graph == graph
        ]
        if len(matches) != 1:
            raise InputError("Expected one flat node in the selected graph")
        return matches[0]


def _descriptions(
    document: object, budget: Budget
) -> list[tuple[str | None, str | None, bool, SourcePath, dict[str, object]]]:
    """Inventory expanded node descriptions without interpreting context syntax."""
    result = []

    def walk(
        value: object,
        path: SourcePath,
        graph: str | None,
        anonymous_graph: bool = False,
    ) -> None:
        """Keep references, literal payloads and graph membership distinct."""
        budget.consume("work")
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index), graph, anonymous_graph)
            return
        if not isinstance(value, dict) or "@value" in value:
            return
        node = cast(dict[str, object], value)
        identifier = node.get("@id")
        identifier = identifier if isinstance(identifier, str) else None
        if set(node) - {"@id", "@index", "@list"}:
            budget.consume("states")
            result.append((identifier, graph, anonymous_graph, path, node))
        for predicate, child in node.items():
            if predicate == "@graph":
                walk(child, (*path, predicate), identifier, identifier is None)
            elif predicate == "@reverse" and isinstance(child, dict):
                for reverse, values in child.items():
                    walk(values, (*path, predicate, reverse), graph, anonymous_graph)
            elif not predicate.startswith("@") or predicate in {"@included", "@list"}:
                walk(child, (*path, predicate), graph, anonymous_graph)

    walk(document, (), None)
    return result


def _inventory(
    expanded: object,
    flattened: object,
    *,
    correspondence: bool,
    budget: Budget,
) -> tuple[tuple[FlatNode, ...], Report | None]:
    """Associate named descriptions only where both graph and ID are stable."""
    original = _descriptions(expanded, budget) if correspondence else []
    paths: dict[tuple[str, str | None], list[SourcePath]] = defaultdict(list)
    issues = IssueCollector(budget)
    checked = 0
    output = _descriptions(flattened, budget)
    keys = {(identifier, graph) for identifier, graph, _, _, _ in output}
    for identifier, graph, anonymous, path, _ in original:
        budget.consume("work")
        checked += 1
        if (
            identifier is None
            or identifier.startswith("_:")
            or anonymous
            or graph is not None
            and graph.startswith("_:")
            or (identifier, graph) not in keys
        ):
            issues.append(
                Issue(
                    "context.flatten_correspondence_unverified",
                    "Expanded node correspondence has no established identity witness",
                    "expanded-input" + location(path),
                    kind="unverified",
                )
            )
        else:
            paths[(identifier, graph)].append(path)
    nodes = []
    for identifier, graph, _, path, value in output:
        if identifier is None:
            raise InputError("Flattening returned a description without a node ID")
        encoded = _json(value)
        budget.consume("bytes", len(encoded.encode("utf-8")))
        nodes.append(
            FlatNode(
                identifier, graph, path, encoded, tuple(paths[(identifier, graph)])
            )
        )
    report = (
        Report(
            "jsonld.flatten_correspondence",
            checked,
            tuple(issues),
            requested=len(original),
            allow_empty=True,
            regime="expanded-node-identity",
            families=("described_node_correspondence",),
        )
        if correspondence
        else None
    )
    return tuple(nodes), report


def flatten_document(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    correspondence: bool = False,
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> FlattenedDocument:
    """Flatten with PyLD and verify exact supported RDF and graph inventory.

    Output uses expanded keys and explicit node IDs; original JSON shape is not
    preserved. bind() supplies concrete model selectors without guessing array
    order. Optional correspondence covers expanded named descriptions, separately
    from RDF preservation; no raw-source mapping or blank identity is inferred.
    All contexts and URL inputs use one owned offline-by-default resource policy.
    """
    if type(correspondence) is not bool:
        raise InputError("correspondence must be a boolean")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InputError("Comparison timeout must be a finite positive number")
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    reports: list[Report] = []
    try:
        issues = IssueCollector(operation)
        dropped: list[str | None] = []
        expanded = expand_document(
            document,
            base=base,
            document_loader=resources,
            on_property_dropped=dropped.append,
        )
        issues.extend(_drop_issues(dropped, frozenset()))
        reports.append(
            Report(
                "jsonld.flatten_input",
                1,
                tuple(issues),
                regime="jsonld",
                families=("expansion_preservation",),
            )
        )
        if not reports[-1].verified:
            return FlattenedDocument(
                None, (), combine_reports("jsonld.flatten", *reports)
            )
        before = prepare_jsonld(
            expanded, document_loader=resources, direction=direction
        )
        reports.append(before.report)
        if before.source is None or not before.report.verified:
            return FlattenedDocument(
                None, (), combine_reports("jsonld.flatten", *reports)
            )
        _consume_work(expanded, operation)
        jsonld = processor()
        try:
            candidate: object = jsonld.flatten(
                expanded,
                options={
                    "base": None,
                    "documentLoader": resources,
                    "processingMode": "json-ld-1.1",
                    "contextResolver": jsonld.ContextResolver(
                        {}, resources, max_context_urls=operation.limits.max_resources
                    ),
                },
            )
        except jsonld.JsonLdError as error:
            raise InputError(str(error)) from error
        candidate = own_json(
            candidate,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
        )
        operation.consume("bytes", _json_size(candidate))
        _consume_work(candidate, operation)
        if not isinstance(candidate, list):
            raise InputError("Flattening did not return a flat node array")
        after = prepare_jsonld(
            candidate, document_loader=resources, direction=direction
        )
        reports.append(after.report)
        if after.source is None or not after.report.verified:
            return FlattenedDocument(
                None, (), combine_reports("jsonld.flatten", *reports)
            )
        reports.append(
            compare_rdf(
                before.source,
                after.source,
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
        )
        report = replace(
            combine_reports("jsonld.flatten", *reports), regime="jsonld-flattening"
        )
        if not report.verified:
            return FlattenedDocument(None, (), report)
        nodes, correlation = _inventory(
            expanded, candidate, correspondence=correspondence, budget=operation
        )
        return FlattenedDocument(_json(candidate), nodes, report, correlation)
    except (LimitExceeded, RecursionError, OverflowError) as error:
        issue = Issue("execution.limit_exceeded", str(error), kind="exhausted")
    except InputError as error:
        issue = Issue("context.flatten_input_error", str(error), kind="input_error")
    reports.append(
        Report("jsonld.flatten", issues=(issue,), requested=1, complete=False)
    )
    return FlattenedDocument(None, (), combine_reports("jsonld.flatten", *reports))
