"""Finite official GO table grammar over maintained CSV transport and exact joins."""

import csv
import io
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from threading import Event
from typing import cast

from rdflib import Graph, Literal, URIRef

from bijux_ontology.data.foreign_references.checking import check_foreign_references
from bijux_ontology.data.foreign_references.preparation import prepare_foreign_targets
from bijux_ontology.data.foreign_references.types import (
    ForeignReferenceContract,
    ForeignReferenceResult,
)
from bijux_ontology.data.keys.types import KeyRecord
from bijux_ontology.declarations.keys import KeyComponent, KeyContract
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.errors import (
    InputError,
    InputFailure,
    LimitExceeded,
    ResourceError,
    SourceSpan,
)
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.reports import Issue, Location, Report

from .types import (
    GoAnnotation,
    GoAnnotationDocument,
    GoAnnotations,
    GoEvidence,
    GoExtension,
    GoGeneProduct,
    GoGeneProductDocument,
)

_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[A-Za-z0-9_.:/-]+\Z", re.ASCII)
_GAF_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[^\s|]+\Z")
_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z", re.ASCII)
_LABEL = re.compile(r"[a-z][a-z_]*\Z", re.ASCII)
_DATE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})?)?\Z",
    re.ASCII,
)
_KEY = URIRef("urn:bijux:ontology:go:subject-key")


def _error(message: str, source: str, line: int, column: int = 1) -> InputError:
    """Attach actual domain-parser source coordinates, not inferred RDF positions."""
    return InputError(
        message,
        failure=InputFailure(
            document_iri=source,
            location=source,
            role="go-annotation",
            span=SourceSpan(
                start_line=line, start_column=column, column_unit="unicode_code_points"
            ),
        ),
    )


def _identifier(value: str, *, strict: bool = True) -> str:
    """Accept the official finite ID grammar without changing its spelling."""
    if not (_ID if strict else _GAF_ID).fullmatch(value):
        raise InputError("Identifier does not match the supported GO CURIE grammar")
    return value


def _date(value: str, *, compact: bool = False) -> None:
    """Use maintained Gregorian date parsing after the exact format guard."""
    if compact:
        if len(value) != 8 or not value.isascii() or not value.isdigit():
            raise InputError("GAF dates require YYYYMMDD")
        value = value[:4] + "-" + value[4:6] + "-" + value[6:]
    elif not _DATE.fullmatch(value):
        raise InputError("GO dates require the supported ISO date or dateTime grammar")
    try:
        datetime.fromisoformat(value) if "T" in value else date.fromisoformat(value)
    except ValueError as error:
        raise InputError("Invalid Gregorian date or dateTime") from error


def _items(
    value: str,
    operation: Operation,
    *,
    required: bool = False,
    identifiers: bool = True,
    strict: bool = True,
) -> tuple[str, ...]:
    """Retain each pipe position, including duplicates, with no set coercion."""
    if not value:
        if required:
            raise InputError("A required field is empty")
        return ()
    operation.consume("states", value.count("|") + 1)
    operation.consume("bytes", 4 * len(value) + 64 * (value.count("|") + 1))
    values = tuple(value.split("|"))
    for item in values:
        operation.consume("work", len(item) + 1)
        if not item:
            raise InputError("An empty grouped field item is invalid")
        if identifiers:
            _identifier(item, strict=strict)
    return values


def _groups(
    value: str,
    operation: Operation,
    *,
    extension: bool = False,
    curie_relation: bool = True,
    strict: bool = True,
) -> tuple[tuple[str | GoExtension, ...], ...]:
    """Preserve disjunction of conjunctions under the official flat grammar."""
    if not value:
        return ()
    operation.consume("states", 2 * (len(value) + 1))
    operation.consume(
        "bytes", 8 * len(value) + 64 * (value.count("|") + value.count(",") + 1)
    )
    groups: list[tuple[str | GoExtension, ...]] = []
    for conjunction in value.split("|"):
        elements: list[str | GoExtension] = []
        for item in conjunction.split(","):
            operation.consume("work", len(item) + 1)
            if not item:
                raise InputError("Empty conjunction/disjunction items are invalid")
            if extension:
                relation, separator, target = item.partition("(")
                if (
                    not separator
                    or not target.endswith(")")
                    or "(" in target
                    or ")" in target[:-1]
                ):
                    raise InputError(
                        "Extensions require relation(target) without nesting"
                    )
                if curie_relation:
                    _identifier(relation)
                elif not _LABEL.fullmatch(relation):
                    raise InputError(
                        "GAF extension relations require explicit relation labels"
                    )
                elements.append(
                    GoExtension(relation, _identifier(target[:-1], strict=strict))
                )
            else:
                elements.append(_identifier(item, strict=strict))
        groups.append(tuple(elements))
    return tuple(groups)


def _properties(value: str, operation: Operation) -> None:
    """Keep property text while rejecting malformed finite key/value syntax."""
    for item in _items(value, operation, identifiers=False):
        key, separator, text = item.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9-]+", key, re.ASCII)
            or not text
        ):
            raise InputError("Properties require nonempty key=value pairs")


def _rows(
    source: str, content: str, kind: str, operation: Operation, issues: IssueCollector
) -> tuple[tuple[str, ...], list[tuple[int, tuple[str, ...]]], bool]:
    """Bound text before CSV allocation and retain actual per-record locations."""
    if type(source) is not str or type(content) is not str:
        raise InputError("GO resources require ordinary source and content strings")
    bound_text(source, operation)
    absolute_iri(source)
    bound_text(content, operation)
    operation.consume("bytes", 12 * len(content) + 128)
    version = "2.2" if kind == "gaf" else "2.0"
    headers: list[str] = []
    rows: list[tuple[int, tuple[str, ...]]] = []
    mandatory: dict[str, str] = {}
    complete = True
    body = False
    for number, line in enumerate(io.StringIO(content), 1):
        operation.consume("work", len(line) + 1)
        operation.consume("states", 2)
        text = line.removesuffix("\n").removesuffix("\r")
        try:
            if not text:
                raise InputError("Blank lines are outside the supported record grammar")
            if kind != "gaf" and (
                not text.isascii() or any(ord(c) < 32 and c != "\t" for c in text)
            ):
                raise InputError("GPAD/GPI require printable ASCII fields")
            if text.startswith("!"):
                if kind != "gaf" and "\t" in text:
                    raise InputError("GO header text cannot contain tab separators")
                if body:
                    raise InputError("Headers must precede annotation/entity records")
                key, separator, value = text[1:].partition(":")
                if separator and key in (
                    kind + "-version",
                    "generated-by",
                    "date-generated",
                ):
                    if key in mandatory:
                        raise InputError("Required header must occur exactly once")
                    declared = value.removeprefix(" ")
                    if key == kind + "-version" and declared != version:
                        mandatory[key] = declared
                        raise ResourceError(
                            "Declared format version is outside this adapter profile"
                        )
                    if key == "generated-by" and not _PREFIX.fullmatch(declared):
                        raise InputError("Invalid generated-by header prefix")
                    if key == "date-generated":
                        _date(declared)
                    mandatory[key] = declared
                headers.append(text)
                continue
            body = True
            if kind + "-version" not in mandatory:
                raise InputError("Missing explicit supported format version")
            if mandatory[kind + "-version"] != version:
                raise ResourceError(
                    "Declared format version is outside this adapter profile"
                )
            if not _PREFIX.fullmatch(
                mandatory.get("generated-by", "")
            ) or not mandatory.get("date-generated"):
                raise InputError("Missing generated-by/date-generated header")
            _date(mandatory["date-generated"])
            expected = 17 if kind == "gaf" else 12 if kind == "gpad" else 11
            if text.count("\t") != expected - 1:
                raise InputError(f"Expected exactly {expected} tab-separated fields")
            operation.consume("states", expected + 1)
            try:
                values = tuple(
                    next(
                        csv.reader(
                            (text,), delimiter="\t", quoting=csv.QUOTE_NONE, strict=True
                        )
                    )
                )
            except csv.Error as error:
                raise ResourceError(
                    "CSV implementation could not preserve this bounded record"
                ) from error
            expected = 17 if kind == "gaf" else 12 if kind == "gpad" else 11
            if len(values) != expected:
                raise InputError(f"Expected exactly {expected} tab-separated fields")
            if any(any(ord(c) < 32 for c in field) for field in values):
                raise InputError(
                    "Control characters are outside the supported field grammar"
                )
            rows.append((number, values))
        except LimitExceeded:
            raise
        except InputError as error:
            complete = False
            qualified = _error(str(error), source, number)
            issues.append(
                Issue(
                    "go.input_unverified"
                    if isinstance(error, ResourceError)
                    else "go.input_invalid",
                    str(error),
                    source,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                    details=Location.from_error(qualified),
                )
            )
    if not body and (
        mandatory.get(kind + "-version") != version
        or not mandatory.get("generated-by")
        or not mandatory.get("date-generated")
    ):
        complete = False
        located = _error("Required format header is missing or invalid", source, 1)
        issues.append(
            Issue(
                "go.input_unverified"
                if mandatory.get(kind + "-version") not in (None, version)
                else "go.input_invalid",
                str(located),
                source,
                kind="unverified"
                if mandatory.get(kind + "-version") not in (None, version)
                else "input_error",
                details=Location.from_error(located),
            )
        )
    return tuple(headers), rows, complete


def _annotation(
    source: str, number: int, fields: tuple[str, ...], kind: str, operation: Operation
) -> GoAnnotation:
    """Decode only supported field roles, without merging evidence by subject."""

    def cell(index: int, required: bool = True) -> str:
        value = fields[index]
        if required and not value:
            raise _error(
                "A required field is empty",
                source,
                number,
                1 + sum(len(v) + 1 for v in fields[:index]),
            )
        return value

    if kind == "gaf":
        database = cell(0)
        if not _PREFIX.fullmatch(database):
            raise InputError("GAF database requires an explicit prefix")
        subject = _identifier(database + ":" + cell(1), strict=False)
        qualifier = _items(cell(3), operation, required=True, identifiers=False)
        relation_tokens = tuple(value for value in qualifier if value != "NOT")
        if (
            len(relation_tokens) != 1
            or len(qualifier) != len(set(qualifier))
            or len(qualifier) > 2
            or not _LABEL.fullmatch(relation_tokens[0])
        ):
            raise InputError("GAF requires one relation, optionally qualified by NOT")
        term = _identifier(cell(4), strict=False)
        if not term.startswith("GO:") or cell(8) not in ("F", "P", "C"):
            raise InputError("GAF requires a GO term and supported aspect")
        if not re.fullmatch(r"[A-Z]+", cell(6), re.ASCII):
            raise InputError("GAF evidence requires an original evidence-code token")
        taxa = _items(cell(12), operation, required=True, strict=False)
        if len(taxa) not in (1, 2) or any(
            not item.startswith("taxon:") or not item[6:].isdigit() for item in taxa
        ):
            raise InputError(
                "GAF requires entity taxon and at most one interacting taxon"
            )
        _date(cell(13), compact=True)
        cell(2)
        cell(11)
        form = _identifier(fields[16], strict=False) if fields[16] else None
        evidence = GoEvidence(
            cell(6),
            "gaf_code",
            _items(cell(5), operation, required=True, strict=False),
            tuple(
                tuple(str(x) for x in group)
                for group in _groups(fields[7], operation, strict=False)
            ),
        )
        extensions = _groups(
            fields[15], operation, extension=True, curie_relation=False, strict=False
        )
        return GoAnnotation(
            source,
            number,
            "gaf",
            subject,
            form,
            relation_tokens[0],
            term,
            "NOT" in qualifier,
            evidence,
            taxa[0],
            taxa[1:],
            cast(tuple[tuple[GoExtension, ...], ...], extensions),
            fields[13],
            cell(14),
            fields,
        )
    subject = _identifier(cell(0))
    if fields[1] not in ("", "NOT"):
        raise InputError("GPAD negation must be empty or NOT")
    relation, term, eco = (
        _identifier(cell(2)),
        _identifier(cell(3)),
        _identifier(cell(5)),
    )
    if not term.startswith("GO:") or not eco.startswith("ECO:"):
        raise InputError("GPAD requires GO and ECO identifiers in their own roles")
    taxa = _items(fields[7], operation)
    if any(
        not value.startswith("NCBITaxon:") or not value[10:].isdigit() for value in taxa
    ):
        raise InputError("GPAD interacting taxa require NCBITaxon identifiers")
    _date(cell(8))
    _properties(fields[11], operation)
    extensions = _groups(fields[10], operation, extension=True)
    evidence = GoEvidence(
        eco,
        "eco",
        _items(cell(4), operation, required=True),
        tuple(tuple(str(x) for x in group) for group in _groups(fields[6], operation)),
    )
    return GoAnnotation(
        source,
        number,
        "gpad",
        subject,
        None,
        relation,
        term,
        fields[1] == "NOT",
        evidence,
        None,
        taxa,
        tuple(
            tuple(x for x in group if isinstance(x, GoExtension))
            for group in extensions
        ),
        fields[8],
        cell(9),
        fields,
    )


def _join(
    annotations: list[GoAnnotation],
    entities: list[GoGeneProduct],
    complete: bool,
    source_complete: bool,
    operation: Operation,
) -> tuple[list[GoAnnotation], ForeignReferenceResult]:
    """Reuse actual exact-key extraction and correlated foreign-reference checking."""
    graph = Graph()
    tables: tuple[tuple[str, Sequence[GoAnnotation | GoGeneProduct]], ...] = (
        ("assertion", annotations),
        ("entity", entities),
    )
    for prefix, items in tables:
        for index, item in enumerate(items):
            operation.consume("states", 2)
            operation.consume("bytes", 256)
            operation.consume("triples")
            graph.add(
                (
                    URIRef(f"urn:bijux:ontology:go:{prefix}:{index}"),
                    _KEY,
                    Literal(
                        item.subject
                        if isinstance(item, GoAnnotation)
                        else item.identifier
                    ),
                )
            )
    operation.consume("states", 4 * (len(annotations) + len(entities)) + 8)
    operation.consume("bytes", 512 * (len(annotations) + len(entities)) + 256)
    prepared = prepare_source(graph, budget=operation)
    sources = tuple(
        KeyRecord(str(i), prepared, URIRef(f"urn:bijux:ontology:go:assertion:{i}"))
        for i in range(len(annotations))
    )
    targets = tuple(
        KeyRecord(str(i), prepared, URIRef(f"urn:bijux:ontology:go:entity:{i}"))
        for i in range(len(entities))
    )
    key = KeyContract("GO subject identifier", (KeyComponent(_KEY),))
    contract = ForeignReferenceContract("GO annotation companion", key, key)
    operation.consume("states", 2 * len(annotations) + len(entities))
    operation.consume("bytes", 256 * (len(annotations) + len(entities)))
    target_index = prepare_foreign_targets(
        targets, contract, complete=complete, budget=operation
    )
    result = check_foreign_references(
        sources, target_index, source_complete=source_complete, budget=operation
    )
    joined = list(annotations)
    for resolution in result.resolutions:
        if resolution.status == "resolved":
            number = int(resolution.source.identifier)
            entity = entities[int(resolution.targets[0].identifier)]
            joined[number] = replace(
                joined[number],
                entity=entity,
                entity_taxon=entity.taxon
                if joined[number].entity_taxon is None
                else joined[number].entity_taxon,
            )
    return joined, result


def read_go_annotations(
    document: GoAnnotationDocument,
    *,
    gpi: GoGeneProductDocument | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> GoAnnotations:
    """Read supplied current formats without fetching or repairing identifiers."""
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    annotations: list[GoAnnotation] = []
    entities: list[GoGeneProduct] = []
    headers: tuple[str, ...] = ()
    children: list[Report] = []
    join = None
    requested = 1
    complete = True
    try:
        if type(document) is not GoAnnotationDocument:
            raise InputError("Supply a typed GAF/GPAD document")
        declared_format = cast(object, document.format)
        if type(declared_format) is not str or document.format not in (
            "gaf",
            "gpad",
        ):
            raise InputError("Supply a typed GAF/GPAD document and explicit format")
        if gpi is not None and type(gpi) is not GoGeneProductDocument:
            raise InputError("Supply a typed explicit GPI companion resource")
        headers, rows, complete = _rows(
            document.source, document.content, document.format, operation, issues
        )
        requested = max(1, len(rows))
        for number, fields in rows:
            try:
                operation.consume("states", 8)
                operation.consume("bytes", 1024)
                annotations.append(
                    _annotation(
                        document.source, number, fields, document.format, operation
                    )
                )
            except LimitExceeded:
                raise
            except InputError as error:
                complete = False
                located = (
                    error
                    if error.failure is not None
                    else _error(str(error), document.source, number)
                )
                issues.append(
                    Issue(
                        "go.input_invalid",
                        str(error),
                        document.source,
                        kind="input_error",
                        details=Location.from_error(located),
                    )
                )
        if document.format == "gpad" and gpi is None:
            raise ResourceError("GPAD requires its explicitly supplied GPI companion")
        if gpi is not None:
            _, entity_rows, entity_complete = _rows(
                gpi.source, gpi.content, "gpi", operation, issues
            )
            for number, fields in entity_rows:
                try:
                    for position in (0, 4, 5):
                        _items(fields[position], operation, required=True)
                    if (
                        not fields[1]
                        or " " in fields[1]
                        or "|" in fields[1]
                        or "|" in fields[2]
                        or not fields[2]
                        or "|" in fields[0]
                        or "|" in fields[5]
                        or not fields[5].startswith("NCBITaxon:")
                        or not fields[5][10:].isdigit()
                    ):
                        raise InputError(
                            "GPI requires one subject, symbol, name and entity taxon"
                        )
                    for position in (3, 6, 7, 8, 9):
                        _items(fields[position], operation, identifiers=position != 3)
                    _properties(fields[10], operation)
                    operation.consume("states", 4)
                    operation.consume("bytes", 256)
                    entities.append(
                        GoGeneProduct(gpi.source, number, fields[0], fields[5], fields)
                    )
                except LimitExceeded:
                    raise
                except InputError as error:
                    entity_complete = False
                    located = _error(str(error), gpi.source, number)
                    issues.append(
                        Issue(
                            "go.input_invalid",
                            str(error),
                            gpi.source,
                            kind="input_error",
                            details=Location.from_error(located),
                        )
                    )
            annotations, join = _join(
                annotations, entities, entity_complete, complete, operation
            )
            children.append(join.report)
            retain_report(join.report, operation, issues)
            complete = complete and entity_complete and join.report.complete
            for resolution in join.resolutions:
                if resolution.status == "resolved":
                    continue
                assertion = annotations[int(resolution.source.identifier)]
                located = _error(
                    "Required companion subject is " + resolution.status,
                    assertion.source,
                    assertion.line,
                )
                issues.append(
                    Issue(
                        "go.gpi_subject_" + resolution.status,
                        "Required companion subject is " + resolution.status,
                        assertion.source,
                        kind="unverified"
                        if resolution.status == "unverified"
                        else "finding",
                        actual=assertion.subject,
                        details=Location.from_error(located),
                    )
                )
        if not annotations:
            complete = False
            issues.append(
                Issue(
                    "go.required_annotations_empty",
                    "No required annotation assertions were established",
                    document.source,
                    kind="unverified",
                )
            )
    except LimitExceeded as error:
        complete = False
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error))
    except InputError as error:
        complete = False
        try:
            issues.append(
                Issue(
                    "go.input_unverified"
                    if isinstance(error, ResourceError)
                    else "go.input_invalid",
                    str(error),
                    error.location,
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    return GoAnnotations._owned(
        source=document,
        gpi=gpi,
        headers=headers,
        annotations=tuple(annotations),
        gene_products=tuple(entities),
        join=join,
        report=Report(
            "go.annotations.read",
            len(annotations),
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
            regime="explicit-current-GO-annotation-formats",
        ),
    )
