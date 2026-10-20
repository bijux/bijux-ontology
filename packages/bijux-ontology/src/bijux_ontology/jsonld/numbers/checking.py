"""Qualify original JSON numeric tokens with maintained decoding and RDF processing."""

import json
import math
from collections.abc import Sequence
from dataclasses import replace
from decimal import Context, Decimal, DecimalException
from typing import Literal as Choice
from typing import cast

from rdflib import Literal, URIRef
from rdflib.namespace import XSD

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.json.paths import SourcePath, bounded_location
from bijux_ontology.json.values import _decode_json
from bijux_ontology.jsonld.processing.preparation import prepare_jsonld
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.literals.checking import check_literal
from bijux_ontology.rdf.terms import bound_node, literal_term

from .types import NumberPolicy, NumericJSON, NumericObservation, NumericProjection


class _Unavailable(InputError):
    """A valid numeric request has no established supported representation."""


def _decimal(token: str, budget: Budget) -> Decimal:
    """Bound coefficient storage before maintained exact decimal construction."""
    bound_text(token, budget)
    budget.consume("states", len(token))
    budget.consume("bytes", 64 + 8 * len(token))
    try:
        value = Decimal(token, context=Context())
    except DecimalException as error:
        raise _Unavailable(
            "Source number exceeds the supported decimal magnitude"
        ) from error
    if not value.is_finite():
        raise _Unavailable("Source number is not a supported finite value")
    return value


def _integral(value: Decimal) -> bool:
    """Test finite decimal integrality without context-dependent arithmetic."""
    digits = value.as_tuple()
    exponent = cast(int, digits.exponent)
    return exponent >= 0 or not any(digits.digits[exponent:])


def _lexical(value: Decimal, datatype: str, budget: Budget) -> str:
    """Reserve expanded decimal text before formatting any coefficient or zeros."""
    parts = value.as_tuple()
    exponent = cast(int, parts.exponent)
    count = len(parts.digits)
    if datatype == str(XSD.integer):
        if not _integral(value):
            raise InputError("Integer projection requires an integral source value")
        size = max(1, count + exponent) + parts.sign
    else:
        size = parts.sign + (
            count + exponent if exponent >= 0 else max(count + 1, 2 - exponent)
        )
    if size > budget.limits.max_bytes:
        raise LimitExceeded("Projected numeric lexical form exceeds max_bytes")
    budget.consume("work", size + count + 1)
    budget.consume("bytes", 4 * size + 64)
    rendered = (
        value.to_integral_value(context=Context())
        if datatype == str(XSD.integer)
        else value
    )
    lexical = format(rendered, "f")
    if datatype == str(XSD.integer):
        lexical = lexical.partition(".")[0]
    bound_text(lexical, budget)
    return lexical


def _projections(
    values: Sequence[NumericProjection], budget: Budget
) -> dict[SourcePath, NumericProjection]:
    """Capture exact projection intent before numeric token resolution."""
    if not isinstance(values, Sequence) or isinstance(
        cast(object, values), str | bytes
    ):
        raise InputError("Numeric projections require a finite typed sequence")
    count = len(values)
    budget.consume("states", count)
    budget.consume("bytes", 64 + 16 * count)
    result = {}
    for index in range(count):
        budget.consume("work")
        try:
            projection = values[index]
        except IndexError as error:
            raise InputError("Projection inventory changed during capture") from error
        if not isinstance(projection, NumericProjection):
            raise InputError("Expected typed NumericProjection declarations")
        where = bounded_location(projection.path, budget)
        bound_text(projection.datatype, budget)
        if any(
            isinstance(part, str) and part.startswith("@") for part in projection.path
        ):
            raise _Unavailable(
                "Keyword payload locations require separate semantic qualification",
                location=where,
            )
        if projection.datatype not in (str(XSD.decimal), str(XSD.integer)):
            raise _Unavailable(
                "Numeric projection supports explicit decimal or integer datatypes",
                location=where,
            )
        if projection.path in result:
            raise InputError("Numeric projection locations must be unique")
        result[projection.path] = projection
    return result


class _Numbers:
    """One bounded operation's original tokens, explicit projections and exactness."""

    def __init__(
        self,
        policy: NumberPolicy,
        projections: dict[SourcePath, NumericProjection],
        budget: Budget,
        issues: IssueCollector,
    ) -> None:
        """Retain only this operation's already qualified policy and bounded state."""
        self.policy = policy
        self.projections = projections
        self.budget = budget
        self.issues = issues
        self.observations: list[NumericObservation] = []
        self.pending: list[tuple[int, int | float, Decimal]] = []
        self.seen: set[SourcePath] = set()
        self.has_context = False

    def key(self, key: str) -> None:
        """Observe context presence during the existing owned decoder traversal."""
        if key == "@context":
            self.has_context = True

    def number(self, token: str, path: SourcePath, integer: bool) -> object:
        """Resolve a maintained raw token at its actual original object/array path."""
        budget = self.budget
        where = bounded_location(path, budget)
        bound_text(token, budget)
        budget.consume("states")
        budget.consume("bytes", 128)
        index = len(self.observations)
        self.observations.append(NumericObservation(path, token, "unavailable", None))
        exact = _decimal(token, budget)
        projection = self.projections.get(path)
        if projection is not None:
            self.seen.add(path)
            lexical = _lexical(exact, projection.datatype, budget)
            report = check_literal(
                literal_term(lexical, datatype=projection.datatype), budget=budget
            )
            retain_report(report, budget, self.issues)
            if not report.verified:
                raise _Unavailable(
                    "Projected literal does not establish datatype validity"
                )
            budget.consume("states")
            self.observations[index] = NumericObservation(
                path,
                token,
                "typed-lexical",
                True,
                projected_datatype=projection.datatype,
            )
            return {"@value": lexical, "@type": projection.datatype}
        try:
            value = int(token) if integer else float(token)
        except (ValueError, OverflowError) as error:
            raise _Unavailable(
                "Source number exceeds the processor's supported numeric magnitude"
            ) from error
        if isinstance(value, float) and not math.isfinite(value):
            raise _Unavailable(
                "Source number exceeds the processor's finite number range"
            )
        decoded = Decimal(value) if type(value) is int else Decimal.from_float(value)
        same = decoded == exact
        budget.consume("states")
        self.observations[index] = NumericObservation(
            path, token, type(value).__name__, same
        )
        if self.policy == "portable-integer":
            if not _integral(exact) or exact.copy_abs() > Decimal(2**53 - 1):
                self.issues.append(
                    Issue(
                        "context.number_not_portable",
                        "Number violates the selected portable integer range contract",
                        where,
                    )
                )
        elif self.policy == "source-exact":
            if not same:
                self.issues.append(
                    Issue(
                        "context.number_not_exact",
                        "Raw number changes on the processor decoding path",
                        where,
                    )
                )
            else:
                budget.consume("states")
                budget.consume("bytes", 64)
                self.pending.append((index, value, exact))
        return value

    def rdf(self) -> None:
        """Check the actual maintained default native-number RDF conversion route."""
        if not self.pending:
            return
        budget = self.budget
        processor()  # Missing optional runtime is an interoperability limitation.
        subject = "urn:bijux:numeric-probe"
        budget.consume("states", len(self.pending) + 1)
        budget.consume("bytes", 128 + 64 * len(self.pending))
        document: dict[str, object] = {"@id": subject}
        for index, value, _ in self.pending:
            budget.consume("work")
            document[f"urn:bijux:numeric:{index}"] = value
        prepared = prepare_jsonld(document, budget=budget)
        retain_report(prepared.report, budget, self.issues)
        if prepared.source is None or not prepared.report.verified:
            raise _Unavailable("Maintained numeric RDF route could not be qualified")
        graph = prepared.source._read(budget=budget)
        for index, _, expected in self.pending:
            budget.consume("work")
            values = list(
                graph.objects(URIRef(subject), URIRef(f"urn:bijux:numeric:{index}"))
            )
            if len(values) != 1 or not isinstance(values[0], Literal):
                raise _Unavailable(
                    "Numeric RDF route returned an unsupported value inventory"
                )
            term = values[0]
            bound_node(term, budget)
            report = check_literal(term, budget=budget)
            retain_report(report, budget, self.issues)
            if not report.verified:
                raise _Unavailable(
                    "Numeric RDF result lacks verified datatype validity"
                )
            actual: Decimal | None
            if term.datatype == XSD.integer:
                actual = Decimal(str(term), context=Context())
            elif term.datatype == XSD.double:
                value = float(str(term))
                actual = Decimal.from_float(value) if math.isfinite(value) else None
            else:
                raise _Unavailable("Numeric RDF route returned an unsupported datatype")
            same = actual == expected
            budget.consume("states")
            self.observations[index] = replace(self.observations[index], rdf_exact=same)
            if not same:
                self.issues.append(
                    Issue(
                        "context.number_rdf_not_exact",
                        "Source value changes on the native-number RDF route",
                        bounded_location(self.observations[index].path, budget),
                    )
                )


def _run(
    payload: str | bytes,
    projections: Sequence[NumericProjection],
    policy: NumberPolicy,
    limits: Limits | None,
    budget: Budget | None,
) -> NumericJSON:
    """Decode once through shared JSON syntax/duplicate/path handling and qualify it."""
    if policy not in ("processor", "portable-integer", "source-exact"):
        raise InputError("Choose an explicit supported numeric input policy")
    if budget is not None and not isinstance(budget, Budget):
        raise InputError("Expected a shared Budget")
    if limits is not None and not isinstance(limits, Limits):
        raise InputError("Expected explicit Limits")
    if budget is not None and limits is not None and budget.limits != limits:
        raise InputError("Numeric limits conflict with the shared Budget")
    operation = budget if budget is not None else Budget(limits)
    issues = IssueCollector(operation)
    state = _Numbers(policy, {}, operation, issues)
    output = None
    checked = 0
    try:
        if not isinstance(payload, str | bytes):
            raise _Unavailable(
                "Raw numeric qualification requires original JSON text or bytes"
            )
        if len(payload) > operation.limits.max_bytes:
            raise LimitExceeded("Raw JSON input exceeds max_bytes")
        if isinstance(payload, str):
            bound_text(payload, operation)
        else:
            operation.consume("work", len(payload) + 1)
            operation.consume("bytes", len(payload))
        state.projections = _projections(projections, operation)
        document = _decode_json(
            payload,
            max_bytes=operation.limits.max_bytes,
            max_depth=operation.limits.max_depth,
            number_hook=state.number,
            key_hook=state.key if state.projections else None,
            budget=operation,
        )
        for path in state.projections.keys() - state.seen:
            issues.append(
                Issue(
                    "context.number_projection_missing",
                    "Selected numeric projection has no numeric source token",
                    bounded_location(path, operation),
                )
            )
        if state.projections and state.has_context:
            raise _Unavailable(
                "Numeric projection requires context-free input; "
                "context aliases and coerced payload roles are not qualified"
            )
        if policy == "source-exact":
            state.rdf()
        checked = 1
        encoded = json.dumps(
            document, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        bound_text(encoded, operation)
        output = encoded
    except LimitExceeded as error:
        issues.exhausted(str(error))
    except (InputError, DecimalException, OverflowError, UnicodeError) as error:
        kind: Choice["unverified", "input_error"] = (
            "unverified"
            if isinstance(error, _Unavailable | ResourceError)
            else "input_error"
        )
        try:
            message = (
                str(error)
                if isinstance(error, InputError)
                else "Numeric representation is unsupported on this processing path"
            )
            bound_text(message, operation)
            where = error.location if isinstance(error, InputError) else ""
            bound_text(where, operation)
            issues.append(
                Issue(
                    "context.number_unverified"
                    if kind == "unverified"
                    else "context.number_input_error",
                    message,
                    where,
                    kind=kind,
                )
            )
        except LimitExceeded as exhausted:
            issues.exhausted(str(exhausted))
    report = Report(
        "JSON numeric input",
        checked,
        tuple(issues),
        requested=1,
        regime=f"raw-json-numbers:{policy}",
        exclusions=("arbitrary context coercion and whole-document RDF preservation",),
    )
    return NumericJSON(
        output if report.verified else None, tuple(state.observations), report
    )


def decode_numeric_json(
    payload: str | bytes,
    *,
    policy: NumberPolicy = "processor",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> NumericJSON:
    """Qualify raw JSON under an explicit ordinary, portable or exact number model.

    Source-exact includes an actual default native-number RDF-route probe, not a
    promise about arbitrary context coercion. Raw token spelling stays evidence;
    exactness concerns numeric value. Decoded Python floats cannot recover tokens.
    """
    return _run(payload, (), policy, limits, budget)


def project_numeric_json(
    payload: str | bytes,
    projections: Sequence[NumericProjection],
    *,
    policy: NumberPolicy = "processor",
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> NumericJSON:
    """Project only explicitly selected numeric data cells to checked lexical values.

    Projection uses original tokens and explicit integer/decimal datatype intent.
    Only context-free documents are supported: any @context key leaves projection
    unverified, including aliases and coerced JSON payloads. Keyword locations are
    not reinterpreted. Other values retain their ordinary selected number model.
    """
    return _run(payload, projections, policy, limits, budget)
