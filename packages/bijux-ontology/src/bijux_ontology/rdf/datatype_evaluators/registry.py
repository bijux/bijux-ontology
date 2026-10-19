"""Finite per-operation custom datatype evaluation without inferred semantics."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from sys import getsizeof
from types import MappingProxyType
from typing import cast

from rdflib import BNode, Literal
from rdflib.namespace import RDF, XSD

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.identifiers import absolute_iri

from .types import (
    DatatypeConversion,
    DatatypeEvaluator,
    DatatypeRelation,
    DatatypeScalar,
    Regime,
    _text,
)


@dataclass(frozen=True, init=False)
class DatatypeRegistry:
    """Own an immutable catalog, evaluating its caller callbacks afresh each time.

    Operation bounds cover supplied terms, callback counts and retained results.
    Ordinary caller code executes in process; no preemption or behavior snapshot
    is promised. Standard XSD/RDF semantics are never replaced by this catalog.
    """

    limits: Limits
    _entries: Mapping[str, DatatypeEvaluator] = field(repr=False)

    def __init__(
        self, evaluators: Sequence[DatatypeEvaluator], *, limits: Limits | None = None
    ) -> None:
        if limits is not None and not isinstance(limits, Limits):
            raise InputError("Expected explicit Limits")
        object.__setattr__(self, "limits", limits or Limits())
        budget = Budget(self.limits)
        if isinstance(cast(object, evaluators), (str, bytes)) or not isinstance(
            evaluators, Sequence
        ):
            raise InputError("Datatype evaluators require a finite sequence")
        count = len(evaluators)
        if count > self.limits.max_states:
            raise LimitExceeded("Datatype catalog exceeds max_states")
        entries: dict[str, DatatypeEvaluator] = {}
        for index in range(count):
            budget.consume("work")
            budget.consume("states")
            try:
                evaluator = evaluators[index]
            except IndexError as error:
                raise InputError("Datatype catalog changed during capture") from error
            if not isinstance(evaluator, DatatypeEvaluator):
                raise InputError("Expected explicit DatatypeEvaluator values")
            _text(evaluator.iri, budget)
            budget.consume("states", len(evaluator.validators))
            if evaluator.iri in entries:
                raise InputError("Datatype catalog contains a duplicate IRI")
            entries[evaluator.iri] = evaluator
        object.__setattr__(self, "_entries", MappingProxyType(entries))

    def _operation(self, budget: Budget | None) -> Budget:
        """Use the caller operation without resetting its counters or limits."""
        if budget is not None and not isinstance(budget, Budget):
            raise InputError("Expected a shared Budget")
        return budget if budget is not None else Budget(self.limits)

    def lookup(
        self, iri: str, *, budget: Budget | None = None
    ) -> DatatypeEvaluator | None:
        """Read one exact bounded catalog key without inferring a missing capability."""
        operation = self._operation(budget)
        if not isinstance(iri, str) or isinstance(iri, BNode | Literal):
            raise InputError("Datatype lookup requires an IRI string")
        _text(iri, operation)
        absolute_iri(iri)
        return self._entries.get(iri)

    def _term(self, value: Literal, budget: Budget) -> DatatypeEvaluator | None:
        """Bound the original RDF term before invoking caller code."""
        if not isinstance(value, Literal):
            raise InputError("Datatype evaluation requires an RDF literal")
        if len(value) > 4096:
            raise LimitExceeded(
                "Custom datatype lexical evaluation exceeds 4096 characters"
            )
        _text(str(value), budget)
        if value.language:
            _text(value.language, budget)
        datatype = str(
            value.datatype or (RDF.langString if value.language else XSD.string)
        )
        return self.lookup(datatype, budget=budget)

    def check(
        self, value: Literal, *, location: str = "", budget: Budget | None = None
    ) -> Report:
        """Conjoin supplied validators, retaining false alongside unknown or failure."""
        operation = self._operation(budget)
        issues = IssueCollector(operation)
        checked = 0
        complete = True
        requested = 1
        anchor = ""
        try:
            _text(location, operation)
            anchor = location
            evaluator = self._term(value, operation)
            if evaluator is None or not evaluator.validators:
                issues.append(
                    Issue(
                        "literal.datatype_unverified",
                        "No sample-validity capability supplied for this datatype",
                        anchor,
                        kind="unverified",
                    )
                )
                checked = 1
            else:
                requested = len(evaluator.validators)
                for index, validator in enumerate(evaluator.validators):
                    operation.consume("work")
                    operation.consume("states")
                    checked += 1
                    failure = False
                    try:
                        result = validator(value)
                    except LimitExceeded:
                        raise
                    except Exception:
                        result = None
                        failure = True
                    if result is True:
                        continue
                    invalid = result is False
                    issues.append(
                        Issue(
                            "literal.lexical_invalid"
                            if invalid
                            else "literal.datatype_unverified",
                            "Supplied validator rejects the original literal"
                            if invalid
                            else "Datatype validator did not establish sample validity"
                            + (" (callback failed)" if failure else ""),
                            anchor,
                            kind="finding" if invalid else "unverified",
                            actual=f"validator {index}",
                        )
                    )
        except LimitExceeded:
            complete = False
            issues.exhausted(
                "Datatype evaluation stopped at an explicit limit", location=anchor
            )
        return Report(
            "literal",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
            regime="consumer-datatype-sample",
        )

    def convert(
        self, value: Literal, *, location: str = "", budget: Budget | None = None
    ) -> DatatypeConversion:
        """Convert a valid sample through its explicit bounded scalar capability."""
        operation = self._operation(budget)
        validation = self.check(value, location=location, budget=operation)
        issues = IssueCollector(operation)
        converted: DatatypeScalar | None = None
        complete = validation.complete
        anchor = location if validation.verified else ""
        if validation.verified:
            try:
                evaluator = self._term(value, operation)
                operation.consume("work")
                candidate = None
                failure = False
                if evaluator is not None and evaluator.converter is not None:
                    try:
                        candidate = evaluator.converter(value)
                    except LimitExceeded:
                        raise
                    except Exception:
                        failure = True
                if (
                    type(candidate) not in (bool, int, Decimal, str, bytes)
                    or isinstance(candidate, Decimal)
                    and not candidate.is_finite()
                ):
                    issues.append(
                        Issue(
                            "literal.conversion_unverified",
                            "No supported converted value established"
                            + (" (callback failed)" if failure else ""),
                            anchor,
                            kind="unverified",
                        )
                    )
                else:
                    operation.consume("states")
                    size = getsizeof(candidate)
                    if size > operation.limits.max_bytes:
                        raise LimitExceeded(
                            "Converted datatype scalar exceeds max_bytes"
                        )
                    operation.consume("bytes", size)
                    if isinstance(candidate, str):
                        _text(candidate, operation)
                    converted = cast(DatatypeScalar, candidate)
            except LimitExceeded:
                complete = False
                issues.exhausted(
                    "Datatype evaluation stopped at an explicit limit",
                    location=anchor,
                )
            except InputError:
                try:
                    issues.append(
                        Issue(
                            "literal.conversion_unverified",
                            "Converted scalar is outside the supported representation",
                            anchor,
                            kind="unverified",
                        )
                    )
                except LimitExceeded:
                    complete = False
                    issues.exhausted(
                        "Datatype evaluation stopped at an explicit limit",
                        location=anchor,
                    )
        report = Report(
            "literal.conversion",
            1,
            tuple(issues),
            requested=1,
            complete=complete,
            regime="consumer-datatype-conversion",
            children=(validation,),
        )
        return DatatypeConversion(
            value,
            converted if report.verified and converted is not None else ABSENT,
            report,
        )

    def relation(
        self,
        actual: str,
        required: str,
        *,
        regime: Regime = "rdf",
        budget: Budget | None = None,
    ) -> DatatypeRelation:
        """Ask only the actual datatype's explicit directional relation evaluator."""
        operation = self._operation(budget)
        raw_regime: object = regime
        if type(raw_regime) is not str or regime not in (
            "application",
            "rdf",
            "owl-dl",
        ):
            raise InputError("Unknown semantic regime")
        if not isinstance(required, str) or isinstance(required, BNode | Literal):
            raise InputError("Datatype relations require named IRI strings")
        _text(required, operation)
        absolute_iri(required)
        evaluator = self.lookup(actual, budget=operation)
        if evaluator is None or evaluator.relation is None:
            return "unknown"
        operation.consume("work")
        operation.consume("states")
        try:
            result = evaluator.relation(required, regime)
        except LimitExceeded:
            raise
        except Exception:
            return "unknown"
        raw_result: object = result
        return (
            cast(DatatypeRelation, raw_result)
            if type(raw_result) is str
            and raw_result in ("equal", "subset", "overlap", "disjoint", "unknown")
            else "unknown"
        )
