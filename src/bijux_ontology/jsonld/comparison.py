"""Compare representative usages through independent controlled JSON-LD operations."""

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace

from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

from ..errors import InputError, LimitExceeded
from ..reports import Issue, Report, combine_reports
from .checking import PreparedUsages, prepare_usages
from .conversion import DirectionPolicy, convert_to_rdf
from .observations import ObservedOccurrence, SourcePath, location
from .resources import DocumentLoader


def _resource(value: str | None) -> str | None:
    return "_:" if isinstance(value, str) and value.startswith("_:") else value


def _signature(item: ObservedOccurrence) -> tuple[object, ...]:
    traits = item.traits
    return (
        item.predicate,
        item.reverse,
        item.container,
        _resource(item.subject),
        _resource(item.graph),
        traits.kind,
        traits.datatype,
        traits.language.lower() if traits.language else None,
        traits.direction,
        _resource(traits.identifier)
        if traits.identifier_kind == "blank"
        else traits.identifier,
        tuple(sorted(traits.types)),
        item.empty,
    )


def _group(items: tuple[ObservedOccurrence, ...]) -> dict[str, set[tuple[object, ...]]]:
    result: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    for item in items:
        result[item.location].add(_signature(item))
    return dict(result)


def _display(values: set[tuple[object, ...]]) -> str:
    return json.dumps(sorted(values, key=repr), ensure_ascii=False)


def compare_context_usages(
    before_document: object,
    after_document: object,
    selectors: Sequence[SourcePath],
    *,
    before_base: str | None = None,
    after_base: str | None = None,
    before_loader: DocumentLoader | None = None,
    after_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> Report:
    """Compare actual selected mappings, coercions, directions and node scopes.

    Documents include their original contexts. Unused definition spelling is
    outside this representative scope. This operation does not establish complete
    serialization equivalence or compatibility for unobserved application data.
    Prepared usages reuse their established expansion and owned resources;
    loader/base overrides for an already prepared side are rejected.
    """
    operation = budget or Budget(limits)
    if limits is not None and limits != operation.limits:
        raise InputError("Explicit limits differ from the operation budget")
    issues = IssueCollector(operation)
    checked = 0
    views: list[PreparedUsages | None] = []
    try:
        for name, document, base, loader in (
            ("before", before_document, before_base, before_loader),
            ("after", after_document, after_base, after_loader),
        ):
            try:
                if isinstance(document, PreparedUsages):
                    if base is not None or loader is not None:
                        raise InputError(
                            "Prepared usages cannot override their base or resources"
                        )
                    views.append(document)
                    continue
                views.append(
                    prepare_usages(
                        document,
                        base=base,
                        document_loader=loader,
                        budget=operation,
                    )
                )
            except LimitExceeded:
                raise
            except InputError as error:
                views.append(None)
                issues.append(
                    Issue(
                        f"context.{name}_input_error",
                        str(error),
                        kind="input_error",
                    )
                )
        if all(view is not None for view in views):
            before, after = views
            assert before is not None and after is not None
            for path in selectors:
                operation.consume("work")
                try:
                    previous, current = before.observe(path), after.observe(path)
                except LimitExceeded:
                    raise
                except InputError as error:
                    issues.append(
                        Issue(
                            "context.invalid_path",
                            str(error),
                            location(path),
                            kind="input_error",
                        )
                    )
                    continue
                checked += 1
                if (
                    previous.limitation
                    or current.limitation
                    or previous.empty
                    or current.empty
                ):
                    issues.append(
                        Issue(
                            "context.change_unverified",
                            previous.limitation
                            or current.limitation
                            or "No representative expanded value",
                            location(path),
                            kind="unverified",
                        )
                    )
                    continue
                if previous.dropped or current.dropped:
                    issues.append(
                        Issue(
                            "context.usage_dropped",
                            "A selected usage does not survive one or both expansions",
                            location(path),
                            expected=str(not previous.dropped),
                            actual=str(not current.dropped),
                        )
                    )
                    continue
                previous_groups, current_groups = (
                    _group(previous.occurrences),
                    _group(current.occurrences),
                )
                for where in sorted(previous_groups.keys() | current_groups.keys()):
                    operation.consume("work")
                    old, new = (
                        previous_groups.get(where, set()),
                        current_groups.get(where, set()),
                    )
                    if old != new:
                        issues.append(
                            Issue(
                                "context.usage_meaning_changed",
                                "Observed mapping or effective value traits changed",
                                where,
                                expected=_display(old),
                                actual=_display(new),
                            )
                        )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    return Report(
        "context_changes",
        checked,
        tuple(issues),
        requested=len(selectors),
        complete=not any(issue.kind == "exhausted" for issue in issues),
        regime="jsonld",
        families=("representative_usages",),
    )


def compare_serializations(
    before_document: object,
    after_document: object,
    *,
    before_base: str | None = None,
    after_base: str | None = None,
    before_loader: DocumentLoader | None = None,
    after_loader: DocumentLoader | None = None,
    direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10.0,
) -> Report:
    """Compare representative RDF meaning after independently verified conversion.

    RDF graph identity, list order and consistent blank-node relationships use
    the maintained bounded dataset comparator. Every conversion scope remains
    inspectable. A lossy or incomplete side cannot establish preservation.
    """
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise InputError("Comparison timeout must be a finite positive number")
    operation = budget or Budget(limits)
    if limits is not None and limits != operation.limits:
        raise InputError("Explicit limits differ from the operation budget")
    reports: list[Report] = []
    prepared: list[PreparedSource] = []
    for name, document, base, loader in (
        ("before", before_document, before_base, before_loader),
        ("after", after_document, after_base, after_loader),
    ):
        converted_ready = False
        try:
            converted = convert_to_rdf(
                document,
                base=base,
                document_loader=loader,
                direction=direction,
                budget=operation,
            )
            reports.append(replace(converted.report, scope=f"serialization.{name}"))
            if not converted.report.verified:
                continue
            converted_ready = True
            prepared.append(
                prepare_rdf(
                    converted.nquads,
                    format="nquads",
                    limits=operation.limits,
                    budget=operation,
                )
            )
        except LimitExceeded as error:
            reports.append(
                Report(
                    f"serialization.{name}",
                    issues=(
                        Issue(
                            "execution.limit_exceeded",
                            str(error),
                            kind="exhausted",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    regime="jsonld_to_rdf",
                )
            )
            break
        except InputError as error:
            reports.append(
                Report(
                    f"serialization.{name}",
                    issues=(
                        Issue(
                            "context.rdf_interoperability_unverified"
                            if converted_ready
                            else "context.input_error",
                            str(error),
                            kind="unverified" if converted_ready else "input_error",
                        ),
                    ),
                    requested=1,
                    complete=False,
                    regime="jsonld_to_rdf",
                )
            )
    if len(prepared) == 2 and all(report.verified for report in reports):
        reports.append(
            compare_rdf(
                prepared[0],
                prepared[1],
                limits=operation.limits,
                budget=operation,
                timeout_seconds=timeout_seconds,
            )
        )
    else:
        reports.append(
            Report(
                "serialization.comparison",
                issues=(
                    Issue(
                        "context.serialization_unverified",
                        "Both representative conversions must preserve their meaning",
                        kind="unverified",
                    ),
                ),
                requested=1,
                complete=False,
                regime="rdf-structure",
                families=("representative_serialization",),
            )
        )
    return combine_reports("serialization_changes", *reports)
