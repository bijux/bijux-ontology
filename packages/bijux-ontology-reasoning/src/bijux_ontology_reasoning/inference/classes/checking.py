"""Bounded complex-expression retrieval using one actual prepared reasoner."""

from threading import Event
from typing import TYPE_CHECKING

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.values import object_value

from ...axioms.codec.decoding import ProjectionDecoder
from ...axioms.codec.encoding import ConstructionEncoder
from ...axioms.types import ClassExpression, NamedEntity, SubClassOf
from ...backend.owlapi.protocol import result_boolean, result_records, result_text
from ...backend.owlapi.receipts import QualifiedReceipt
from ..entailment.encoding import UnsupportedQuery, encode_axiom
from ..sessions.types import PreparedReasoningTheory
from .types import ClassQueryGroup, ClassQueryResult

if TYPE_CHECKING:
    from ..sessions.runtime import NativeSession

_THING = "http://www.w3.org/2002/07/owl#Thing"
_NOTHING = "http://www.w3.org/2002/07/owl#Nothing"


def _groups(
    raw: object,
    destination: list[ClassQueryGroup],
    seen: set[NamedEntity],
    operation: Budget,
) -> None:
    """Adopt paid native nodes incrementally, retaining an honest partial prefix."""
    decoder = ProjectionDecoder(operation)
    for value in result_records(raw, operation):
        item = object_value(value, location="native class equivalence node")
        operation.consume("states", 2)
        operation.consume("bytes", 256)
        top, bottom, complete = (
            result_boolean(item.get(name)) for name in ("top", "bottom", "complete")
        )
        members = []
        try:
            for encoded in result_records(item.get("members"), operation):
                operation.consume("states", 3)
                operation.consume("bytes", 192)
                member = decoder.entity(encoded, 1, ("class",))
                if member in seen:
                    raise ResourceError("Native class query repeats a named member")
                seen.add(member)
                members.append(member)
        except LimitExceeded:
            destination.append(ClassQueryGroup(tuple(members), top, bottom, False))
            raise
        if complete and (
            top != any(member.iri == _THING for member in members)
            or bottom != any(member.iri == _NOTHING for member in members)
        ):
            raise ResourceError("Native class node flags contradict complete members")
        destination.append(ClassQueryGroup(tuple(members), top, bottom, complete))


def query_classes(
    session: "NativeSession",
    expression: ClassExpression,
    *,
    direct: bool = False,
    max_results: int = 10_000,
    theory: PreparedReasoningTheory | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: Event | None = None,
) -> ClassQueryResult:
    """Retrieve named strict subclasses/equivalents without parsing label strings."""
    operation = session._operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    children: list[Report] = []
    subclasses: list[ClassQueryGroup] = []
    equivalents: list[ClassQueryGroup] = []
    captured = None
    bound_theory: PreparedReasoningTheory | None = None
    receipt: QualifiedReceipt | None = None
    capability = None
    satisfiable = None
    native_error = None
    complete = False
    consistency = Report("owl.consistency", requested=1, complete=False)
    try:
        with session._locked(operation):
            bound_theory = session.theory
            if (
                type(direct) is not bool
                or type(max_results) is not int
                or max_results <= 0
            ):
                raise InputError(
                    "Select boolean hierarchy depth and a positive result cap"
                )
            operation.consume("states", 12)
            operation.consume("bytes", 2048)
            if isinstance(expression, str):
                raise UnsupportedQuery(
                    "No string/Manchester/label resolver is selected; "
                    "supply a typed expression"
                )
            wrapper = SubClassOf(expression, NamedEntity("class", _THING), ())
            wire = ConstructionEncoder(operation).node(wrapper)
            owned = ProjectionDecoder(operation).axiom(wire)
            if not isinstance(owned, SubClassOf):
                raise ResourceError("Class query construction lost its class role")
            captured = owned.sub_class
            encode_axiom(owned, operation)
            data = session._execute(
                {
                    "operation": "classes",
                    "axioms": [wire],
                    "direct": direct,
                    "max_results": min(max_results, operation.remaining("states")),
                },
                operation,
                issues,
                children,
                theory,
            )
            capability, receipt, consistency = session._header(
                data, operation, issues, children
            )
            if receipt.raw_consistent is not True:
                raise ResourceError(
                    "Ordinary class retrieval requires a consistent theory"
                )
            value = object_value(data.get("class_query"), location="native class query")
            complete = result_boolean(value.get("complete"))
            native_error = result_text(value.get("error"), operation, optional=True)
            satisfiable = (
                None
                if value.get("satisfiable") is None
                else result_boolean(value["satisfiable"])
            )
            seen: set[NamedEntity] = set()
            _groups(value.get("groups"), subclasses, seen, operation)
            equivalent_raw = value.get("equivalents")
            if equivalent_raw is not None:
                _groups([equivalent_raw], equivalents, seen, operation)
            if complete and (
                native_error is not None
                or satisfiable is None
                or len(equivalents) != 1
                or any(not node.complete for node in (*subclasses, *equivalents))
            ):
                raise ResourceError(
                    "Native complete enumeration lacks required coverage"
                )
            if complete and equivalents[0].bottom != (satisfiable is False):
                raise ResourceError(
                    "Native satisfiability contradicts equivalent bottom membership"
                )
            if data["status"] == "exhausted":
                complete = False
                raise LimitExceeded(
                    "Native class enumeration exhausted its result allowance"
                )
            if not complete:
                raise ResourceError(
                    "Native class expression enumeration is unsupported or incomplete"
                )
    except LimitExceeded as error:
        complete = False
        for child in children:
            issues.retain_completed(child)
        issues.exhausted(str(error), location="owl.class_query")
    except (InputError, OSError) as error:
        complete = False
        session._failure(error, issues)
    report = Report(
        "owl.class_query",
        int(receipt is not None and receipt.raw_consistent is not None) + int(complete),
        tuple(issues),
        requested=2,
        complete=complete,
        children=tuple(children),
        regime="prepared-owl2-dl-hermit",
    )
    return ClassQueryResult(
        captured,
        direct,
        tuple(subclasses),
        equivalents[0] if equivalents else None,
        satisfiable,
        receipt.raw_consistent if receipt is not None else None,
        native_error,
        bound_theory.identity if bound_theory is not None else None,
        bound_theory.documents if bound_theory is not None else (),
        receipt.loaders if receipt is not None else (),
        receipt.reasoner if receipt is not None else None,
        capability,
        consistency,
        report,
    )
