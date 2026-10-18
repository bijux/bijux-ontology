"""Assess JSON-LD preservation and convert through the maintained RDF processor."""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, cast

from pyoxigraph import Literal as RdfLiteral
from pyoxigraph import RdfFormat, parse

from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report
from .expansion import _drop_issues, _validate_allowed_drops, expand_document
from .observations import location, occurrences
from .resources import (
    ContextInputError,
    DocumentLoader,
    _consume_work,
    context_resources,
)
from .runtime import processor

DirectionPolicy = Literal["i18n-datatype", "compound-literal"]


@dataclass(frozen=True)
class RDFConversion:
    """Standard N-Quads and the independently assessed preservation outcome."""

    nquads: str
    report: Report


@dataclass(frozen=True)
class _DatasetConversion:
    """Owned processor output shared by the RDF dataset and N-Quads boundaries."""

    dataset: dict[str, object] | None
    nquads: str
    report: Report


def preservation_issues(
    expanded: object,
    *,
    direction: DirectionPolicy | None,
    collector: IssueCollector | None = None,
    on_direction: Callable[[], None] | None = None,
) -> tuple[Issue, ...]:
    """Find contributions the chosen standard RDF conversion cannot retain.

    JSON-LD index annotations are intentionally not RDF assertions. JSON literal
    contents remain opaque, and list conversion is delegated to the processor.
    """
    issues: list[Issue] = collector if collector is not None else []

    def identifier(value: object, path: tuple[str | int, ...], role: str) -> None:
        """Require concrete RDF identifiers without rewriting their spelling."""
        if not isinstance(value, str):
            return
        if value.startswith("_:") and role != "predicate":
            return
        try:
            absolute_iri(value)
        except InputError:
            issues.append(
                Issue(
                    "context.rdf_identifier_loss",
                    f"The {role} is not representable in standard RDF",
                    location(path),
                    actual=value,
                )
            )

    def walk(value: object, path: tuple[str | int, ...] = ()) -> None:
        """Inspect expanded structural roles without looking inside literals."""
        if isinstance(value, list):
            for index, child in enumerate(cast(list[object], value)):
                walk(child, (*path, index))
            return
        if not isinstance(value, dict):
            return
        node = cast(dict[str, object], value)
        if "@value" in node:
            if node.get("@direction") is not None and on_direction is not None:
                on_direction()
            if node.get("@direction") is not None and direction is None:
                issues.append(
                    Issue(
                        "context.rdf_direction_loss",
                        "Direction-sensitive literals require an RDF direction policy",
                        location(path),
                    )
                )
            datatype = node.get("@type")
            if datatype not in (None, "@json"):
                identifier(datatype, (*path, "@type"), "datatype")
            language = node.get("@language")
            if isinstance(language, str):
                try:
                    language_tag(language)
                except InputError:
                    issues.append(
                        Issue(
                            "context.rdf_language_invalid",
                            "The language tag is not representable in standard RDF",
                            location(path),
                            actual=language,
                        )
                    )
            return
        if "@id" in node:
            identifier(
                node["@id"],
                (*path, "@id"),
                "graph name" if "@graph" in node else "identifier",
            )
        for key, child in node.items():
            if key == "@type":
                for item in child if isinstance(child, list) else [child]:
                    identifier(item, (*path, key), "type")
            elif key in {"@graph", "@included", "@reverse", "@list"}:
                walk(child, (*path, key))
            elif not key.startswith("@"):
                identifier(key, (*path, key), "predicate")
                walk(child, (*path, key))

    walk(expanded)
    return tuple(issues)


def literal_preservation_issues(
    expanded: object,
    nquads: str,
    *,
    budget: Budget,
    collector: IssueCollector,
) -> None:
    """Check retained explicit literal terms using the actual standard RDF output.

    The comparison concerns RDF term identity, not source quoting or native JSON
    numeric spelling. It inspects parsed literals, never unrelated text tokens,
    and does not fabricate an output-edge trace for blank nodes. RDF duplicate
    statements have set semantics; the same missing term can affect several
    established expanded-document locations.
    """
    actual: set[tuple[str, str, str | None]] = set()
    try:
        for quad in parse(input=nquads, format=RdfFormat.N_QUADS):
            budget.consume("work")
            if isinstance(quad.object, RdfLiteral):
                term = quad.object
                actual.add((term.value, term.datatype.value, term.language))
    except SyntaxError as error:
        collector.append(
            Issue(
                "context.rdf_output_unverified",
                str(error),
                kind="unverified",
            )
        )
        return
    stack: list[tuple[object, tuple[str | int, ...]]] = [(expanded, ())]
    while stack:
        budget.consume("work")
        value, path = stack.pop()
        if isinstance(value, list):
            stack.extend((item, (*path, index)) for index, item in enumerate(value))
        elif isinstance(value, dict):
            node = cast(dict[str, object], value)
            if "@value" not in node:
                stack.extend((item, (*path, key)) for key, item in node.items())
                continue
            lexical = node["@value"]
            if (
                not isinstance(lexical, str)
                or node.get("@type") == "@json"
                or "@direction" in node
            ):
                continue
            language = node.get("@language")
            language = language.lower() if isinstance(language, str) else None
            datatype = node.get("@type") or (
                "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString"
                if language
                else "http://www.w3.org/2001/XMLSchema#string"
            )
            expected = (lexical, str(datatype), language)
            if expected not in actual:
                collector.append(
                    Issue(
                        "context.rdf_literal_loss",
                        "An expanded literal term did not retain its lexical identity",
                        location(path),
                        expected=repr(expected),
                    )
                )


def _direction_identity_issues(
    expanded: object, budget: Budget, collector: IssueCollector
) -> None:
    """Qualify direction-distinct sibling identity using maintained equality.

    Expansion-local owner/graph tokens never become converted blank identifiers.
    List positions remain separate; only unordered values on the same RDF edge
    can encounter the processor's direction-insensitive node-map deduplication.
    """
    seen: dict[tuple[object, ...], dict[str | None, dict[str, object]]] = {}
    reported: set[tuple[object, ...]] = set()
    compare = processor().JsonLdProcessor.compare_values
    for item in occurrences(expanded, budget=budget):
        budget.consume("work")
        raw = item.value
        if (
            item.container != "unordered"
            or not isinstance(raw, dict)
            or "@value" not in raw
            or raw.get("@type") == "@json"
        ):
            continue
        base = {key: value for key, value in raw.items() if key != "@direction"}
        spelling = json.dumps(base, ensure_ascii=False, sort_keys=True)
        budget.consume("bytes", len(spelling.encode("utf-8")))
        key = (
            item.subject,
            item.subject_identity,
            item.graph,
            item.graph_identity,
            item.anonymous_graph,
            item.predicate,
            item.reverse,
            spelling,
        )
        if key in reported:
            continue
        direction = raw.get("@direction")
        direction = direction if isinstance(direction, str) else None
        if key not in seen:
            budget.consume("states")
            seen[key] = {}
        previous = seen[key]
        if direction in previous:
            continue
        for prior in previous.values():
            budget.consume("work")
            if compare(prior, raw):
                collector.append(
                    Issue(
                        "context.rdf_direction_unverified",
                        "Maintained conversion does not distinguish "
                        "directional sibling literal identities",
                        item.subject or "",
                        kind="unverified",
                        evidence_json=json.dumps(
                            {"predicate": item.predicate, "graph": item.graph}
                        ),
                    )
                )
                budget.consume("states")
                reported.add(key)
                break
        if key not in reported:
            budget.consume("states")
            previous[direction] = raw


def _convert_to_rdf_dataset(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve: bool = True,
    direction: DirectionPolicy | None = None,
) -> _DatasetConversion:
    """Convert through controlled public processing and assess RDF preservation.

    With preserve=True an unrepresentable contribution returns no partial RDF.
    preserve=False returns the processor output with every diagnosed loss retained.
    Expanded RDF locations describe the expanded document, not source pointers.
    """
    if type(preserve) is not bool or direction not in (
        None,
        "i18n-datatype",
        "compound-literal",
    ):
        raise InputError("Invalid RDF conversion policy")
    _validate_allowed_drops(allowed_drops)
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    dropped: list[str | None] = []
    issues = IssueCollector(operation)
    directional = False

    def observed_direction() -> None:
        """Retain only whether the existing preservation walk saw a direction."""
        nonlocal directional
        directional = True

    try:
        expanded = expand_document(
            document,
            base=base,
            document_loader=resources,
            on_property_dropped=dropped.append,
        )
        preservation_issues(
            expanded,
            direction=direction,
            collector=issues,
            on_direction=observed_direction,
        )
        if directional and direction is not None:
            _direction_identity_issues(expanded, operation, issues)
        issues.extend(_drop_issues(dropped, allowed_drops))
    except LimitExceeded as error:
        issues.exhausted(str(error))
        return _DatasetConversion(
            None,
            "",
            Report(
                "jsonld_to_rdf",
                issues=tuple(issues),
                requested=1,
                complete=False,
                regime="jsonld_to_rdf",
            ),
        )
    report = Report(
        "jsonld_to_rdf",
        checked=1,
        issues=tuple(issues),
        regime="jsonld_to_rdf",
        families=("rdf_representability", "expansion_preservation"),
        exclusions=tuple(
            f"omitted observed term: {term}" for term in sorted(allowed_drops)
        ),
    )
    if preserve and report.exit_code() != 0:
        return _DatasetConversion(None, "", report)
    jsonld = processor()
    options: dict[str, object] = {
        "base": None,
        "documentLoader": resources,
        "contextResolver": jsonld.ContextResolver(
            {}, resources, max_context_urls=operation.limits.max_resources
        ),
        "produceGeneralizedRdf": False,
    }
    if direction is not None:
        options["rdfDirection"] = direction
    try:
        _consume_work(expanded, operation)
        dataset: object = jsonld.to_rdf(expanded, options)
        if not isinstance(dataset, dict):
            raise ContextInputError("RDF conversion did not return a standard dataset")
        for _graph in dataset:
            operation.consume("work")
        converted: object = jsonld.JsonLdProcessor.to_nquads(dataset)
        if isinstance(converted, str):
            operation.consume("bytes", len(converted.encode("utf-8")))
            operation.consume("triples", converted.count("\n"))
            literal_preservation_issues(
                expanded, converted, budget=operation, collector=issues
            )
    except (LimitExceeded, OverflowError, RecursionError) as error:
        issues.exhausted(str(error))
        return _DatasetConversion(
            None,
            "",
            Report(
                "jsonld_to_rdf",
                issues=tuple(issues),
                requested=1,
                complete=False,
                regime="jsonld_to_rdf",
            ),
        )
    except jsonld.JsonLdError as error:
        raise ContextInputError(str(error)) from error
    if not isinstance(converted, str):
        raise ContextInputError("RDF conversion did not return standard N-Quads")
    report = Report(
        "jsonld_to_rdf",
        checked=1,
        issues=tuple(issues),
        regime="jsonld_to_rdf",
        families=("rdf_representability", "expansion_preservation", "literal_identity"),
        exclusions=tuple(
            f"omitted observed term: {term}" for term in sorted(allowed_drops)
        ),
    )
    if preserve and (
        report.exit_code() != 0 or any(issue.kind == "unverified" for issue in issues)
    ):
        return _DatasetConversion(None, "", report)
    return _DatasetConversion(cast(dict[str, object], dataset), converted, report)


def convert_to_rdf(
    document: object,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    allowed_drops: frozenset[str] = frozenset(),
    limits: Limits | None = None,
    budget: Budget | None = None,
    preserve: bool = True,
    direction: DirectionPolicy | None = None,
) -> RDFConversion:
    """Return standard N-Quads with an independent preservation assessment.

    N-Quads cannot retain empty named graphs. Use the public prepare_jsonld API for an
    owned dataset representation retaining those graph identities.
    """
    resources = context_resources(document_loader, limits=limits, budget=budget)
    operation = resources.budget
    converted = _convert_to_rdf_dataset(
        document,
        base=base,
        document_loader=resources,
        allowed_drops=allowed_drops,
        budget=operation,
        preserve=preserve,
        direction=direction,
    )
    if converted.dataset is None:
        return RDFConversion("", converted.report)
    issues = IssueCollector(operation)
    try:
        for name, triples in converted.dataset.items():
            if name != "@default" and not triples:
                issues.append(
                    Issue(
                        "context.rdf_empty_graph_loss",
                        "Standard N-Quads cannot retain an empty named graph",
                        actual=name,
                    )
                )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    report = replace(
        converted.report,
        issues=converted.report.issues + tuple(issues),
        complete=converted.report.complete
        and not any(issue.kind == "exhausted" for issue in issues),
    )
    if preserve and (
        report.exit_code() != 0
        or any(issue.kind == "unverified" for issue in report.issues)
    ):
        return RDFConversion("", report)
    return RDFConversion(converted.nquads, report)
