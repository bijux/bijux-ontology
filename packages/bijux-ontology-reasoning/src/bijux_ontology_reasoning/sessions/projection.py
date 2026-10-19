"""Finite selected inferred axioms as qualified ordinary RDF artifacts."""

import time
from typing import TYPE_CHECKING, cast

from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.limits import IssueCollector, retain_report
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Report

from ..axioms.changes.encoding import ConstructionEncoder
from ..axioms.types import NamedEntity, ObjectProperty, ObjectQuantifier, SubClassOf
from ..entailment.encoding import encode_axiom
from ..entailment.types import EntailmentCheck
from .types import (
    InferenceProjection,
    InferenceSelection,
    InferredExistential,
    PreparedReasoningTheory,
)

if TYPE_CHECKING:
    from .checking import ReasoningSession


def _selection(
    selected: InferenceSelection, operation: Operation
) -> tuple[SubClassOf, ...]:
    """Qualify exact explicit roles and prepay products before enumeration."""
    if type(selected) is not InferenceSelection:
        raise InputError("Projection requires an ordinary InferenceSelection")
    if type(selected.families) is not tuple or not selected.families:
        raise InputError("Select nonempty inference families")
    seen_families: set[str] = set()
    for family in selected.families:
        if type(cast(object, family)) is not str:
            raise InputError("Inference family names require text")
        bound_text(family, operation)
        if family not in ("subclass", "existential") or family in seen_families:
            raise InputError("Select distinct supported inference families")
        operation.consume("states")
        seen_families.add(family)
    for values, kind in (
        (selected.owners, NamedEntity),
        (selected.superclasses, NamedEntity),
        (selected.properties, ObjectProperty),
        (selected.fillers, NamedEntity),
    ):
        if type(values) is not tuple:
            raise InputError("Inference domains require exact finite tuples")
        operation.consume("states", len(values) * 2)
        operation.consume("bytes", 128 + 64 * len(values))
        seen = set()
        for value in values:
            if type(value) is not kind or (
                type(value) is NamedEntity and value.kind != "class"
            ):
                raise InputError(
                    "Inference domains need typed named classes/object properties"
                )
            bound_text(value.iri, operation)
            if type(value) is ObjectProperty and type(value.inverse) is not bool:
                raise InputError("Inference inverse-property flags require booleans")
            if value in seen:
                raise InputError("Inference domains cannot contain duplicate members")
            seen.add(value)
    if not selected.owners:
        raise InputError("Inference projection requires selected owners")
    count = 0
    if "subclass" in seen_families:
        if not selected.superclasses:
            raise InputError("Subclass projection requires a finite superclass domain")
        count += len(selected.owners) * len(selected.superclasses)
    if "existential" in seen_families:
        if not selected.properties or not selected.fillers:
            raise InputError(
                "Existential projection requires finite property/filler domains"
            )
        count += len(selected.owners) * len(selected.properties) * len(selected.fillers)
    operation.consume("work", count)
    operation.consume("states", count * 8)
    operation.consume("bytes", 128 + count * 768)
    queries = []
    for owner in selected.owners:
        if "subclass" in seen_families:
            for target in selected.superclasses:
                query = SubClassOf(owner, target, ())
                encode_axiom(query, operation)
                queries.append(query)
        if "existential" in seen_families:
            for prop in selected.properties:
                for filler in selected.fillers:
                    query = SubClassOf(
                        owner, ObjectQuantifier("some", prop, filler), ()
                    )
                    encode_axiom(query, operation)
                    queries.append(query)
    return tuple(queries)


def project_inferences(
    session: "ReasoningSession",
    selection: InferenceSelection,
    *,
    expected: PreparedReasoningTheory | None,
    operation: Operation,
) -> InferenceProjection:
    """Keep quantifiers and unknown pairs; never create class-instance assertions."""
    issues = IssueCollector(operation)
    children: list[Report] = []
    bound = session.theory if expected is None else expected
    identity = bound.identity if type(bound) is PreparedReasoningTheory else ""
    checked: EntailmentCheck | None = None
    edges: list[InferredExistential] = []
    source = None
    content = None
    complete = False
    try:
        queries = _selection(selection, operation)
        # Publication storage is reserved before a native answer can establish it.
        operation.consume("states", 8 * len(queries) + 16)
        operation.consume("bytes", 512 + 768 * len(queries))
        checked = session.check_entailments(
            queries,
            theory=bound,
            budget=operation,
            timeout=max(0.000001, operation.deadline - time.monotonic()),
            cancel=operation.cancel,
        )
        # A false entailment is a completed pair observation, not an objection to
        # projecting just positive consequences. Required unknown remains fatal.
        negative_findings = {
            id(issue)
            for answer in checked.answers
            if answer.outcome == "not_entailed"
            for issue in answer.report.issues
        }
        coverage = Report(
            "owl.inference_pair_coverage",
            len(checked.answers),
            tuple(i for i in checked.report.issues if id(i) not in negative_findings),
            requested=len(queries),
            complete=checked.report.complete,
            children=(checked.consistency_report,),
        )
        children.append(coverage)
        try:
            retain_report(coverage, operation, issues)
        except LimitExceeded:
            issues.retain_completed(coverage)
            raise
        positives = []
        for answer in checked.answers:
            operation.consume("work")
            if answer.outcome != "entailed":
                continue
            axiom = cast(SubClassOf, answer.axiom)
            positives.append(axiom)
            if type(axiom.super_class) is ObjectQuantifier:
                restriction = axiom.super_class
                edges.append(
                    InferredExistential(
                        cast(NamedEntity, axiom.sub_class),
                        restriction.property,
                        cast(NamedEntity, restriction.filler),
                        answer,
                    )
                )
        if (
            checked.consistent is not True
            or not checked.report.complete
            or any(answer.outcome == "unknown" for answer in checked.answers)
        ):
            raise ResourceError(
                "Selected inference pairs are incomplete or unqualified"
            )
        encoder = ConstructionEncoder(operation)
        wire = [encoder.node(axiom) for axiom in positives]
        with session._locked(operation):
            data = session._execute(
                {"operation": "projection", "axioms": wire},
                operation,
                issues,
                children,
                bound,
            )
            _, receipt, _ = session._header(data, operation, issues, children)
            if (
                receipt.raw_consistent is not True
                or data.get("projection_complete") is not True
            ):
                raise ResourceError("Native inference serialization is unqualified")
            raw = data.get("content")
            if type(raw) is not str:
                raise ResourceError("Native inference serialization omitted RDF bytes")
            bound_text(raw, operation)
            operation.consume("bytes", len(raw) * 4 + 128)
            content = raw.encode("utf-8")
            if len(content) > operation.limits.max_bytes:
                raise LimitExceeded("Inference artifact exceeds max_bytes")
            source = prepare_rdf(
                RdfSource(
                    content,
                    format="turtle",
                    document_iri="urn:bijux:ontology:inference:" + identity,
                ),
                budget=operation,
            )
            complete = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="owl.inference_projection")
    except (InputError, OSError) as error:
        session._failure(error, issues)
    # The original required-axiom check remains in answers; its observed negative
    # pairs are data here. Coverage and native export are required projection work.
    report = Report(
        "owl.inference_projection",
        int(complete),
        tuple(issues),
        requested=1,
        complete=complete,
        children=tuple(children),
        regime="finite-selected-owl-consequences",
    )
    return InferenceProjection(
        selection,
        identity,
        checked,
        tuple(edges),
        source if complete else None,
        report,
        content if complete else None,
    )
