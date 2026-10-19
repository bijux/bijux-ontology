"""Actual SDRF validation plus required completion against explicit local resources."""

from enum import Enum
from typing import Any

from bijux_ontology.errors import InputError, LimitExceeded
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Location, Report

from .consumer import (
    _parameters_supported,
    _runtime,
    _schema_validator,
    local_sdrf_client,
)
from .terminology import SdrfTerminology, _fold
from .types import SdrfCell, SdrfCheck, SdrfDiagnostic, SdrfObligation

_PARAMS = frozenset(
    (
        "ontologies",
        "error_level",
        "use_ols_cache_only",
        "allow_not_applicable",
        "allow_not_available",
        "description",
        "examples",
        "parent_accession",
        "recommend_nt_ac",
    )
)
_SENTINELS = frozenset(("not applicable", "not available", "norm"))


def _own(value: Any, budget: Budget, models: tuple[type, ...], depth: int = 0) -> Any:
    """Bound schema metadata before any maintained model copy or validation."""
    budget.consume("work")
    budget.consume("states")
    if depth > budget.limits.max_depth:
        raise LimitExceeded("SDRF schema exceeds max_depth")
    if isinstance(value, Enum):
        return _own(value.value, budget, models, depth + 1)
    if isinstance(value, str):
        bound_text(value, budget)
        return value[:]
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > 64:
            raise InputError(
                "SDRF schema integers must fit the supported finite metadata profile"
            )
        return value
    if type(value) is dict:
        budget.consume("states", len(value))
        result = {}
        for key in value:
            if type(key) is not str:
                raise InputError("SDRF schema metadata requires string keys")
            bound_text(key, budget)
            result[key] = _own(value[key], budget, models, depth + 1)
        return result
    if type(value) in (list, tuple):
        budget.consume("states", len(value))
        return [
            _own(value[index], budget, models, depth + 1) for index in range(len(value))
        ]
    if type(value) in models:
        return {
            name: _own(getattr(value, name), budget, models, depth + 1)
            for name in type(value).model_fields
        }
    raise InputError("Unsupported SDRF schema metadata cannot be copied implicitly")


def _location(cell: SdrfCell | None, budget: Budget) -> tuple[str, Location | None]:
    """Use actual physical cell ordinals, never a guessed reordered column index."""
    if cell is None:
        return "", None
    bound_text(cell.name, budget)
    text = f"row[{cell.row + 1}]/column[{cell.column + 1}]"
    bound_text(text, budget)
    return text, Location(source="SDRF", path=(cell.row, cell.column))


def _semantic(
    cell: SdrfCell,
    validator: Any,
    terminology: SdrfTerminology,
    budget: Budget,
    issues: IssueCollector,
    unresolved: set[tuple[int, int]],
) -> None:
    """Qualify consumer-parsed identities and open-source uncertainty per cell."""

    def issue(message: str, *, finding: bool = False) -> None:
        """Retain the original cell and its actual ordinal only after bounds."""
        if not finding:
            budget.consume("states")
            unresolved.add((cell.row, cell.column))
        where, details = _location(cell, budget)
        bound_text(cell.value, budget)
        issues.append(
            Issue(
                "sdrf.semantic_mismatch" if finding else "sdrf.unverified",
                message,
                where,
                kind="finding" if finding else "unverified",
                actual=cell.value,
                details=details,
            )
        )

    if not cell.value.strip():
        return  # Original SDRF required/empty-cell validators own this obligation.
    try:
        parsed = validator.ontology_term_parser(cell.value)
    except ValueError:
        return  # The unchanged consumer reports malformed NT/AC syntax.
    label = parsed.get("NT")
    if label in _SENTINELS:
        return  # Original column policy determines whether a sentinel is allowed.
    names = validator.ontologies
    selected_names = {_fold(name, budget) for name in names}
    budget.consume("work", len(terminology._catalogues))
    budget.consume("states", len(terminology._catalogues))
    supplied_names = {name for name, _, _ in terminology._catalogues}
    if not selected_names.issubset(supplied_names):
        issue("A selected ontology has no supplied local resource")
        return
    matches = terminology.search(label, None, budget) if label else ()
    budget.consume("work", len(matches))
    budget.consume("states", len(matches))
    budget.consume("bytes", 64 + 8 * len(matches))
    selected = tuple(
        term for term in matches if not names or term.ontology in selected_names
    )
    accession = parsed.get("AC")
    candidates = []
    if accession:
        bound_text(accession, budget)
        term = terminology.resolve(accession, budget)
        if term is None:
            colliding = terminology._accessions.get(_fold(accession, budget), ())
            issue(
                "Accession binding is ambiguous or absent from an incomplete catalogue",
                finding=not colliding and terminology.closed(names, budget),
            )
            return
        if names and term.ontology not in selected_names:
            issue("Accession is outside the explicitly selected ontology", finding=True)
            return
        candidates.append(term)
        if label is None or _fold(label, budget) not in {
            _fold(value, budget) for value in term.labels
        }:
            issue(
                "The original NT/AC pair does not match the selected accession",
                finding=bool(term.labels),
            )
    else:
        candidates.extend(selected)
        if not candidates:
            issue(
                "The label has no qualified local terminology match",
                finding=terminology.closed(names, budget),
            )
    if validator.parent_accession and candidates:
        results = []
        for term in candidates:
            budget.consume("work")
            results.append(
                terminology.under_parent(
                    term.accession, validator.parent_accession, budget
                )
            )
        if True not in results:
            issue(
                "Accession is outside the asserted parent"
                if None not in results
                else "Asserted parent membership cannot be completed",
                finding=None not in results,
            )


def check_sdrf(
    table: Any,
    schema: Any,
    terminology: SdrfTerminology,
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> SdrfCheck:
    """Run the real effective schema and local ontology provider without networking.

    Input tables come from the maintained ``read_sdrf`` parser; schemas may be
    normal registry-combined definitions. Structural rules remain owned by SDRF.
    Unknown/custom validator obligations and open catalogue misses cannot certify
    semantic completion. Maintained validators execute normally in-process;
    adapter budgets do not claim preemption of arbitrary consumer computation.
    """
    runtime = _runtime()
    if (
        type(table) is not runtime.dataframe
        or type(table.df) is not runtime.frame
        or type(schema) is not runtime.schema
        or not isinstance(terminology, SdrfTerminology)
    ):
        raise InputError(
            "Expected parsed SDRF table, effective schema and prepared terminology"
        )
    if limits is not None and budget is not None and limits != budget.limits:
        raise InputError("SDRF limits conflict with the shared budget")
    operation = budget if budget is not None else Budget(limits)
    issues, consumer_issues = IssueCollector(operation), IssueCollector(operation)
    cells: list[SdrfCell] = []
    diagnostics: list[SdrfDiagnostic] = []
    obligations: list[SdrfObligation] = []
    checked = 0
    requested = 1
    children = []
    unresolved: set[tuple[int, int]] = set()
    try:
        retain_report(terminology.report, operation, issues)
        children.append(terminology.report)
        effective = runtime.schema.model_validate(
            _own(schema, operation, runtime.models)
        )
        if (
            effective.extends
            or effective.mutually_exclusive_with
            or effective.excludes
            or not effective.usable_alone
        ):
            issues.append(
                Issue(
                    "sdrf.unverified",
                    "Schema composition needs an effective combined definition",
                    kind="unverified",
                )
            )
        rows, columns = table.df.shape
        operation.consume("states", rows * columns + columns)
        operation.consume("bytes", 128 + 128 * rows * columns)
        names = []
        ordinals: dict[str, int] = {}
        for index in range(columns):
            name = table.df.columns[index]
            if type(name) is not str:
                raise InputError("SDRF column names require original strings")
            bound_text(name, operation)
            if name in ordinals:
                raise InputError(
                    "Ambiguous duplicate SDRF column names require explicit resolution"
                )
            ordinals[name] = index
            names.append(name)
        data = []
        for row in range(rows):
            values = []
            for column in range(columns):
                operation.consume("work")
                value = table.df.iloc[row, column]
                if type(value) is not str:
                    raise InputError("Parsed SDRF cells must retain string values")
                bound_text(value, operation)
                values.append(value)
                cells.append(SdrfCell(row, column, names[column], value))
            data.append(values)
        # The constructor owns a new ordinary DataFrame; no caller frame is changed.
        owned = runtime.dataframe(runtime.frame(data, columns=names))
        registry = runtime.registry(schema_dir="")
        registry.schemas[effective.name] = effective
        client = local_sdrf_client(terminology, budget=operation)
        runner = _schema_validator(runtime, registry, client)
        requested = max(
            1,
            rows
            * sum(
                config.validator_name == "ontology"
                for column in effective.columns
                for config in column.validators
                if column.name in ordinals
            ),
        )
        for column in effective.columns:
            if column.name not in ordinals:
                continue
            actual_column = ordinals[column.name]
            for validation, config in enumerate(column.validators):
                implementation = runtime.get_validator(config.validator_name)
                if config.validator_name != "ontology":
                    if (
                        implementation is None
                        or implementation.__module__ != "sdrf_pipelines.sdrf.validators"
                    ):
                        issues.append(
                            Issue(
                                "sdrf.unverified",
                                "An unsupported column validator remains uncovered",
                                kind="unverified",
                            )
                        )
                    continue
                if not _parameters_supported(config.params):
                    issues.append(
                        Issue(
                            "sdrf.unverified",
                            "Invalid ontology parameter shapes remain uncovered",
                            kind="unverified",
                        )
                    )
                    continue
                operation.consume("states")
                operation.consume("bytes", 192)
                obligations.append(
                    SdrfObligation(
                        actual_column,
                        column.name,
                        validation,
                        column.requirement.value,
                        tuple(config.params.get("ontologies", ())),
                        config.params.get("parent_accession"),
                        column.allow_not_applicable,
                        column.allow_not_available,
                    )
                )
                unsupported = set(config.params) - _PARAMS
                if unsupported or column.allow_pooled or column.allow_anonymized:
                    issues.append(
                        Issue(
                            "sdrf.unverified",
                            "Unsupported ontology parameters or sentinels",
                            kind="unverified",
                        )
                    )
                validator = runtime.ontology(
                    params={
                        **config.params,
                        "allow_not_applicable": column.allow_not_applicable,
                        "allow_not_available": column.allow_not_available,
                    },
                    client=client,
                )
                for row in range(rows):
                    operation.consume("work")
                    _semantic(
                        cells[row * columns + actual_column],
                        validator,
                        terminology,
                        operation,
                        issues,
                        unresolved,
                    )
                    checked += 1
        for config in effective.validators:
            implementation = runtime.get_validator(config.validator_name)
            if (
                config.validator_name == "ontology"
                or implementation is None
                or implementation.__module__ != "sdrf_pipelines.sdrf.validators"
            ):
                issues.append(
                    Issue(
                        "sdrf.unverified",
                        "A global ontology or custom validator remains uncovered",
                        kind="unverified",
                    )
                )
        if checked == 0:
            issues.append(
                Issue(
                    "sdrf.unverified",
                    "No applicable ontology cell obligations were extracted",
                    kind="unverified",
                )
            )
        operation.consume(
            "work",
            (rows * columns + 1)
            * (len(effective.columns) + len(effective.validators) + 1),
        )
        try:
            native = runner.validate(
                owned, effective.name, use_ols_cache_only=False, skip_ontology=False
            )
        except LimitExceeded:
            raise
        except Exception:
            issues.append(
                Issue(
                    "sdrf.unverified",
                    "The maintained SDRF validator could not complete",
                    kind="unverified",
                )
            )
            native = []
        operation.consume("states", len(native))
        for error in native:
            operation.consume("work")
            code = (
                error.error_code.value if error.error_code is not None else "consumer"
            )
            row, name = error.row, error.column
            for text in (code, error.message, error.severity, name, error.value):
                if text is not None:
                    bound_text(text, operation)
            if type(row) is not int or row < -1 or row >= rows:
                raise InputError("Consumer diagnostics must reference an actual row")
            cell = (
                cells[row * columns + ordinals[name]]
                if row >= 0 and name in ordinals
                else None
            )
            where, details = _location(cell, operation)
            diagnostics.append(
                SdrfDiagnostic(
                    code, error.message, error.severity, row, name, error.value
                )
            )
            unavailable = (
                error.error_code is not None
                and error.error_code.name.startswith("ONTOLOGY_")
                and (
                    error.severity != "error"
                    or cell is not None
                    and (cell.row, cell.column) in unresolved
                )
            )
            consumer_issues.append(
                Issue(
                    "sdrf.consumer",
                    error.message,
                    where,
                    kind="unverified" if unavailable else "finding",
                    severity=error.severity,
                    actual=cell.value if cell else error.value,
                    details=details,
                )
            )
    except LimitExceeded as error:
        issues.exhausted(str(error))
    consumer_report = Report(
        "sdrf.consumer",
        len(diagnostics) if diagnostics else checked,
        tuple(consumer_issues),
        allow_empty=True,
    )
    try:
        retain_report(consumer_report, operation, issues)
    except LimitExceeded as error:
        issues.exhausted(str(error))
    children.append(consumer_report)
    return SdrfCheck(
        tuple(cells),
        tuple(obligations),
        tuple(diagnostics),
        consumer_report,
        Report(
            "sdrf.local",
            checked,
            tuple(issues),
            requested=requested,
            children=tuple(children),
            regime="asserted-local-sdrf-terminology",
        ),
    )
