"""Actual ROBOT generation qualified by complete typed row and consumer checks."""

import csv
import io
import re
from dataclasses import replace
from threading import Event
from typing import cast

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.patches.correspondence import Operation
from bijux_ontology.reports import Issue, Report

from ..axioms.changes.encoding import ConstructionEncoder
from ..axioms.inspection import inspect_axioms
from ..axioms.types import (
    AnnotationAssertion,
    AxiomInspection,
    Declaration,
    NativeLiteral,
    ProjectedAxiom,
)
from ..backend.bundles import PROFILES
from ..backend.types import OwlProfile, OwlProfileBackend
from ..documents.conversion import convert_ontology
from ..documents.preparation import prepare_owl_document
from ..documents.types import OwlDocument
from ..entailment.checking import check_entailments
from .backend import execute_table
from .preparation import LABEL, PreparedTable, prepare_table
from .types import (
    TableCompilation,
    TableRowReceipt,
    TemplateBackend,
    TemplateColumn,
    TemplateIdentifier,
    TemplateTerm,
)


def _bindings(
    prepared: PreparedTable, inspected: AxiomInspection, operation: Operation
) -> None:
    """Require an entirely local declaration/label vocabulary with exact roles."""
    if (
        not inspected.report.verified
        or len(inspected.documents) != 1
        or inspected.documents[0].imports
    ):
        raise ResourceError(
            "Template binding scope is not fully qualified and import-free"
        )
    operation.consume("bytes", 512 + 256 * len(inspected.axioms))
    operation.consume("states", 4 * len(inspected.axioms))
    declared = set()
    labels: dict[str, set[str]] = {}
    for record in inspected.axioms:
        operation.consume("work")
        axiom = record.axiom
        if isinstance(axiom, Declaration):
            declared.add(axiom.entity)
        elif (
            isinstance(axiom, AnnotationAssertion)
            and axiom.property == LABEL
            and isinstance(axiom.value, NativeLiteral)
        ):
            labels.setdefault(axiom.value.lexical, set()).add(axiom.subject.iri)
        else:
            raise ResourceError(
                "Template binding profile supports only declarations and labels"
            )
    for term in prepared.terms:
        operation.consume("work")
        if term.entity not in declared:
            raise InputError(
                "A term binding is not declared in its explicit native role",
                location="bindings." + term.token,
            )
        if term.token in labels and labels[term.token] != {term.entity.iri}:
            raise InputError(
                "A local label is ambiguous or bound to another entity",
                location="bindings." + term.token,
            )
    operation.consume("bytes", 128 * len(prepared.terms))
    operation.consume("states", len(prepared.terms))
    term_map = {term.token: term.entity for term in prepared.terms}
    for column in prepared.columns:
        if column.template.startswith("SC ") and column.template.endswith(" some %"):
            token = column.template[3:-7]
            entity = term_map[token]
            if labels.get(token) != {entity.iri}:
                raise InputError(
                    "Restriction properties require their unique explicit local label",
                    location="table[2]." + column.header,
                )
    existing = {entity.iri for entity in declared}
    if any(row.iri in existing for row in prepared.rows):
        raise InputError(
            "Generated class identifiers collide with supplied binding entities",
            location="table",
        )


def _native_errors(
    raw: bytes | None,
    receipts: list[TableRowReceipt],
    operation: Operation,
    issues: IssueCollector,
) -> int:
    """Retain only actual maintained row/cell coordinates and diagnostics."""
    if not raw:
        return 0
    operation.consume("bytes", 8 * len(raw))
    operation.consume("work", 4 * len(raw))
    try:
        text = raw.decode("utf-8")
        capacity = text.count(",") + text.count("\n") + 1
        operation.consume("bytes", 256 * capacity)
        operation.consume("states", capacity)
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        if next(reader, None) != ["table", "cell", "rule ID", "message"]:
            raise ResourceError("ROBOT returned an unsupported diagnostic table")
        operation.consume("bytes", 128 + 128 * len(receipts))
        operation.consume("states", len(receipts))
        operation.consume("work", len(receipts))
        positions = {receipt.row: index for index, receipt in enumerate(receipts)}
        count = 0
        for row in reader:
            if len(row) != 4:
                raise ResourceError("ROBOT returned a malformed row diagnostic")
            match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", row[1])
            if match is None:
                raise ResourceError("ROBOT omitted its actual cell coordinate")
            number = int(match[2])
            index = positions.get(number)
            if index is None:
                raise ResourceError(
                    "Native diagnostic is outside the supplied row inventory"
                )
            issue = Issue(
                "templates.native_row_error",
                row[3],
                f"table[{number}].{row[1]}",
                actual=row[2],
                kind="input_error",
            )
            issues.append(issue)
            previous = receipts[index].output_report
            report = Report(
                "templates.row_output",
                requested=1,
                complete=False,
                issues=(issue,),
                children=() if previous is None else (previous,),
            )
            receipts[index] = replace(receipts[index], output_report=report)
            count += 1
        return count
    except (UnicodeDecodeError, csv.Error, ValueError) as error:
        if isinstance(error, csv.Error) and str(error).startswith(
            "field larger than field limit"
        ):
            raise LimitExceeded(
                "Native diagnostic CSV field allowance exceeded"
            ) from error
        if isinstance(error, InputError):
            raise
        raise ResourceError(
            "ROBOT row diagnostics cannot be decoded faithfully"
        ) from error


def _coverage(
    prepared: PreparedTable,
    inspected: AxiomInspection,
    receipts: list[TableRowReceipt],
    operation: Operation,
    issues: IssueCollector,
) -> None:
    """Account every requested assertion and every actual exported axiom."""
    count = len(inspected.axioms) + sum(len(row.axioms) for row in prepared.rows)
    operation.consume("bytes", 512 + 256 * count)
    operation.consume("states", 3 * count)
    operation.consume("work", 4 * count)
    actual = {record.axiom for record in inspected.axioms}
    expected = {axiom for row in prepared.rows for axiom in row.axioms}
    allowed = {term.entity for term in prepared.terms}
    for row in prepared.rows:
        first = row.axioms[0]
        if isinstance(first, Declaration):
            allowed.add(first.entity)
    for index, (row, receipt) in enumerate(zip(prepared.rows, receipts, strict=True)):
        missing = any(axiom not in actual for axiom in row.axioms)
        local: tuple[Issue, ...] = ()
        if missing:
            issue = Issue(
                "templates.row_output_missing",
                "Native output does not realize every requested typed row assertion",
                f"table[{row.row}]",
                kind="unverified",
            )
            issues.append(issue)
            local = (issue,)
        receipts[index] = replace(
            receipt,
            output_report=Report(
                "templates.row_output",
                checked=0 if missing else 1,
                requested=1,
                complete=not missing,
                issues=local,
            ),
        )
    for axiom in actual:
        if axiom in expected:
            continue
        if (
            isinstance(axiom, Declaration)
            and not axiom.annotations
            and axiom.entity in allowed
        ):
            continue
        issues.append(
            Issue(
                "templates.unrequested_output",
                "Native output contains an assertion outside the declared row profile",
                kind="unverified",
            )
        )


def compile_table_template(
    runtime: TemplateBackend,
    backend: OwlProfileBackend,
    table: bytes | str,
    *,
    columns: tuple[TemplateColumn, ...],
    bindings: OwlDocument,
    identifiers: tuple[TemplateIdentifier, ...],
    terms: tuple[TemplateTerm, ...],
    prefixes: tuple[tuple[str, str], ...],
    ontology_iri: str,
    postconditions: tuple[ProjectedAxiom, ...],
    delimiter: str = "csv",
    profiles: tuple[OwlProfile, ...] = ("owl2-dl",),
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    heap_mb: int = 256,
    cancel: Event | None = None,
) -> TableCompilation:
    """Compile the declared finite native dialect with complete row accounting.

    Class-only rows support ID, LABEL, TYPE, SC %, named-property existential
    templates and plain string annotations. Logical cells are exact explicit
    local class tokens; native ROBOT owns Manchester substitution and escaping.
    The supplied import-free Functional Syntax bindings contain declarations and
    labels only. Output remains withheld on any unknown, error or partial work.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    prepared = None
    rows: list[TableRowReceipt] = []
    children: list[Report] = []
    binding_inspection = inspection = conversion = obligations = None
    version = None
    output = None
    complete = False
    try:
        encoder = ConstructionEncoder(operation)
        chosen: list[OwlProfile] = []
        for value in encoder.sequence(profiles):
            if type(value) is not str:
                raise InputError("Template profiles require supported plain strings")
            encoder.text(value)
            if value not in PROFILES or value in chosen:
                raise InputError("Choose distinct supported native profiles")
            chosen.append(cast(OwlProfile, value))
        if not chosen:
            raise InputError("Template compilation requires selected native profiles")
        checks, _ = encoder.axioms(postconditions)
        if not checks:
            raise InputError(
                "Template compilation requires nonempty consumer obligations"
            )
        prepared = prepare_table(
            table,
            delimiter,
            columns,
            bindings,
            identifiers,
            terms,
            prefixes,
            ontology_iri,
            operation,
            rows,
            issues,
        )
        operation.consume("bytes", 2048 + 1024 * len(rows))
        operation.consume("states", 32 + 4 * len(rows))
        if not all(row.input_report.verified for row in rows):
            raise InputError(
                "Not all required table rows passed input qualification",
                location="table",
            )
        binding_inspection = inspect_axioms(
            backend, prepared.bindings, budget=operation, heap_mb=heap_mb
        )
        children.append(binding_inspection.report)
        retain_report(binding_inspection.report, operation, issues)
        _bindings(prepared, binding_inspection, operation)
        native = execute_table(
            runtime,
            prepared.content,
            prepared.bindings.content,
            prepared.suffix,
            prepared.prefixes,
            prepared.ontology_iri,
            operation,
            heap_mb,
        )
        version = native.identity
        error_count = _native_errors(native.errors, rows, operation, issues)
        if native.returncode != 0 or error_count:
            raise ResourceError(
                "Native template execution failed; partial output is not accepted"
            )
        if not native.output:
            raise ResourceError("Native template execution did not produce an ontology")
        candidate = prepare_owl_document(
            native.output,
            format="functional",
            document_iri=prepared.ontology_iri,
            budget=operation,
        )
        inspection = inspect_axioms(
            backend, candidate, budget=operation, heap_mb=heap_mb
        )
        children.append(inspection.report)
        retain_report(inspection.report, operation, issues)
        if not inspection.report.verified:
            raise ResourceError(
                "Generated output has no complete typed native inspection"
            )
        if (
            len(inspection.documents) != 1
            or inspection.documents[0].ontology_iri != prepared.ontology_iri
            or inspection.documents[0].imports
            or inspection.documents[0].version_iri is not None
            or inspection.documents[0].annotations
        ):
            raise ResourceError("Generated output changed its declared ontology scope")
        _coverage(prepared, inspection, rows, operation, issues)
        if any(
            row.output_report is None or not row.output_report.verified for row in rows
        ) or any(issue.code == "templates.unrequested_output" for issue in issues):
            raise ResourceError(
                "Generated output does not cover the exact requested row profile"
            )
        conversion = convert_ontology(
            backend,
            candidate,
            format="functional",
            profiles=tuple(chosen),
            budget=operation,
            heap_mb=heap_mb,
        )
        children.append(conversion.report)
        retain_report(conversion.report, operation, issues)
        obligations = check_entailments(
            backend, candidate, checks, budget=operation, heap_mb=heap_mb
        )
        children.append(obligations.report)
        retain_report(obligations.report, operation, issues)
        complete = conversion.report.complete and obligations.report.complete
        if conversion.report.verified and obligations.report.verified:
            output = candidate
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, OSError) as error:
        try:
            issues.append(
                Issue(
                    "templates.unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "templates.input_error",
                    str(error),
                    getattr(error, "location", ""),
                    kind="unverified"
                    if isinstance(error, ResourceError | OSError)
                    else "input_error",
                )
            )
        except LimitExceeded as stopped:
            issues.exhausted(str(stopped))
    for report in children:
        issues.retain_completed(report)
    for row in rows:
        issues.retain_completed(row.input_report)
        if row.output_report is not None:
            issues.retain_completed(row.output_report)
    report = Report(
        "templates.compilation",
        regime="native-table-template",
        families=("row-axioms", "owl-profiles", "consumer-entailments"),
        checked=sum(
            row.output_report is not None and row.output_report.verified for row in rows
        ),
        requested=max(1, len(rows)),
        complete=complete,
        issues=tuple(issues),
        children=tuple(children),
    )
    return TableCompilation(
        None if prepared is None else prepared.content,
        () if prepared is None else prepared.columns,
        None if prepared is None else prepared.bindings,
        tuple(rows),
        version,
        binding_inspection,
        inspection,
        conversion,
        obligations,
        output,
        report,
    )
