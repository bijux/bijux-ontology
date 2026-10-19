"""Checked owned RDF extraction for explicitly supplied CSVW-described text."""

import csv
from collections.abc import Mapping

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, IssueKind, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource

from .conversion import UnsupportedCsvw, convert, metadata, resources
from .types import CsvwCell, CsvwCheck, CsvwExtraction


def extract_csvw(
    description: object,
    tables: Mapping[str, str],
    *,
    base: str,
    checks: tuple[CsvwCheck, ...] = (),
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> CsvwExtraction:
    """Extract one explicit offline CSVW table through maintained native APIs.

    Supported metadata is an inline single-table description with physical named
    columns, explicit aboutUrl/propertyUrl mappings, optional valueUrl, scalar
    string/integer/decimal/boolean/anyURI values, column null/default policies,
    inherited required policies,
    unordered separators and primaryKey. Dialect selection supports header,
    delimiter, quoteChar and doubleQuote. Other rules remain required unknown.
    The URL-to-text inventory is authoritative; no resource is ever fetched.

    Conversion success qualifies only this finite mapping. Selected callbacks
    check the actual owned RDF; no column name or string appearance invents an
    RDF predicate, IRI value, uniqueness promise or ontology consistency claim.
    """
    if budget is not None and (
        not isinstance(budget, Budget) or limits is not None and limits != budget.limits
    ):
        raise InputError("Use a shared CSVW Budget with matching limits")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    cells: list[CsvwCell] = []
    source: PreparedSource | None = None
    completed = False
    check_reports: list[Report] = []
    conversion: Report | None = None
    requested_checks = 0
    try:
        if type(checks) is not tuple:
            raise InputError("CSVW checks require an immutable finite tuple")
        requested_checks = len(checks)
        operation.consume("states", requested_checks)
        names: set[str] = set()
        for check in checks:
            if not isinstance(check, CsvwCheck):
                raise InputError("Expected an explicit CsvwCheck")
            bound_text(check.name, operation)
            if check.name in names:
                raise InputError("CSVW check names must be distinct")
            names.add(check.name)
        table, dialect, reader = metadata(description, base, operation)
        supplied = resources(tables, operation)
        url = str(table.url.resolve(table.base))
        bound_text(url, operation)
        if url not in supplied:
            raise UnsupportedCsvw("Required CSVW table resource was not supplied")
        graph = convert(table, dialect, reader, supplied[url], operation, issues, cells)
        if not issues:
            source = prepare_rdf(graph, base=base, budget=operation)
        completed = True
        conversion = Report(
            "csvw-conversion",
            checked=max(1, len(cells)),
            issues=tuple(issues),
            regime="csvw-explicit-mapping",
        )
        if source is not None:
            for check in checks:
                operation.consume("states")
                raw = check.check(source, operation)
                if not isinstance(raw, Report):
                    raise InputError("CSVW callbacks must return an ordinary Report")
                retain_report(raw, operation, issues)
                check_reports.append(raw)
    except (LimitExceeded, RecursionError):
        issues.exhausted("CSVW extraction or selected checks exceeded operation limits")
    except (ResourceError, InputError, ValueError, csv.Error) as error:
        kind: IssueKind = (
            "unverified" if isinstance(error, ResourceError) else "input_error"
        )
        try:
            issues.append(
                Issue(
                    "csvw.extraction_unverified"
                    if kind == "unverified"
                    else "csvw.input_invalid",
                    str(error)
                    if isinstance(error, UnsupportedCsvw) and not completed
                    else "CSVW metadata or native conversion could not be qualified",
                    kind=kind,
                )
            )
        except LimitExceeded:
            issues.exhausted("CSVW diagnostic exceeded operation limits")
    if conversion is None:
        conversion = Report(
            "csvw-conversion",
            checked=len(cells),
            requested=max(1, len(cells)),
            issues=tuple(issues),
            complete=False,
            regime="csvw-explicit-mapping",
        )
    finished_checks = len(check_reports)
    report = Report(
        "csvw-extraction",
        checked=1 + finished_checks if completed else 0,
        requested=1 + requested_checks,
        issues=tuple(issues),
        complete=completed and finished_checks == requested_checks,
        regime="csvw-explicit-mapping",
        children=(conversion, *check_reports),
    )
    return CsvwExtraction(
        source, tuple(cells), conversion, tuple(check_reports), report
    )
