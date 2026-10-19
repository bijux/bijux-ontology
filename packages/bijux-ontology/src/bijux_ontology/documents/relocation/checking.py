"""Checked document relocation preserves selected source form and resolved RDF."""

import json
import math
from collections.abc import Sequence
from typing import Literal, cast

from rdflib import BNode, Variable
from rdflib import Literal as RdfLiteral

from bijux_ontology._jsonpaths import bound_path
from bijux_ontology._jsonschema import operation, owned_json
from bijux_ontology.documents.export import RDFExport
from bijux_ontology.documents.forms.checking import check_document_form
from bijux_ontology.documents.forms.types import DocumentFormCheck, FormRequirement
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.jsonld.checking import PreparedUsages, prepare_usages
from bijux_ontology.jsonld.comparison import compare_observed_usages
from bijux_ontology.jsonld.conversion import DirectionPolicy
from bijux_ontology.jsonld.expectations import ContextExpectation
from bijux_ontology.jsonld.observations import ObservedUsage
from bijux_ontology.jsonld.resources import ContextResources, DocumentLoader
from bijux_ontology.limits import Budget, IssueCollector, Limits, retain_report
from bijux_ontology.rdf.comparison import compare_rdf
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.patches.correspondence import _json_bound
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.reports import Issue, Location, Report

from .preparation import _Unsupported, candidate, declarations, resources
from .types import DocumentRelocation


def _serialize(document: object, budget: Budget) -> str:
    """Reserve escaped JSON before allocating the exact candidate representation."""
    bound = _json_bound(document, budget)
    if bound > budget.limits.max_bytes:
        raise LimitExceeded("Relocated JSON representation exceeds its byte profile")
    budget.consume("bytes", bound)
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _observations(
    before: PreparedUsages,
    after: PreparedUsages,
    selected: tuple[ContextExpectation, ...],
    budget: Budget,
) -> Report:
    """Compare actual source associations without guessing blank correspondence."""
    paths: set[tuple[str | int, ...]] = set()
    pairs: list[tuple[str, ObservedUsage, ObservedUsage]] = []
    for expected in selected:
        path = expected.path
        bound_path(path, budget)
        if path in paths:
            continue
        budget.consume("states", 2)
        budget.consume("bytes", 128)
        paths.add(path)
        pairs.append((f"usage:{len(pairs)}", before.observe(path), after.observe(path)))
    return compare_observed_usages(pairs, budget=budget)


def relocate_document(
    document: object,
    *,
    old_url: str,
    new_url: str,
    policy: Literal["pin-old-base"],
    requirements: Sequence[FormRequirement],
    expectations: Sequence[ContextExpectation] = (),
    document_loader: DocumentLoader | None = None,
    rdf_direction: DirectionPolicy | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout_seconds: float = 10,
) -> DocumentRelocation:
    """Prepare an owned finite-profile relocation, without writing any destination.

    The explicit policy prepends the old base and resolves only root @context
    string/flat-array resource references against the old document URL. Original
    data keys, containers, values and relative identifier spellings stay intact.
    Null resets, nested/scoped/aliased contexts and non-object document roots are
    outside this finite transformation profile. Supplied resource snapshots keep
    their actual logical/effective bases in one shared ContextResources owner.

    Nonempty caller-selected form requirements and optional occurrence contracts
    are checked on both sides through source-form contracts. Whole resolved RDF
    must compare in exact supported term/graph identity. Raw form/semantic reports,
    observations and sources remain separate from accepted candidate bytes. A failing or
    unknown baseline cannot establish preservation. Comparison timeout bounds
    maintained RDF comparison, not arbitrary caller loader execution.
    """
    inherited = (
        document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None
    )
    selected = operation(limits, budget if budget is not None else inherited)
    issues = IssueCollector(selected)
    old = new = None
    before_form: DocumentFormCheck | None = None
    after_form: DocumentFormCheck | None = None
    before_source = after_source = None
    usage_report = rdf_report = None
    output = None
    children: list[Report] = []
    checked, requested = 0, 4
    complete = False
    try:
        if type(timeout_seconds) is int and timeout_seconds.bit_length() > 63:
            raise InputError("RDF comparison timeout exceeds its finite profile")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise InputError("RDF comparison timeout must be finite and positive")
        for value in (old_url, new_url):
            if isinstance(cast(object, value), (BNode, RdfLiteral, Variable)):
                raise InputError("Document URLs require named IRI text")
            bound_text(value, selected)
            absolute_iri(value)
        old, new = str(old_url), str(new_url)
        bound_text(policy, selected)
        if policy != "pin-old-base":
            raise _Unsupported("Select the explicit pin-old-base preservation policy")
        if rdf_direction not in (None, "i18n-datatype", "compound-literal"):
            raise InputError("Select a supported explicit RDF direction policy")
        forms, expected = declarations(requirements, expectations, selected)
        requested += bool(expected)
        owned, _ = owned_json(document, selected)
        if not isinstance(owned, dict):
            raise _Unsupported("Relocation supports an owned JSON-LD root object")
        moved = candidate(cast(dict[str, object], owned), old, selected)
        serialized = _serialize(moved, selected)
        checked = 1
        # Raw results have paid slots before any loader/check can return them.
        selected.consume("states", 8)
        selected.consume("bytes", 512)
        loader = resources(document_loader, selected)
        before = prepare_usages(
            owned, base=old, document_loader=loader, budget=selected
        )
        before_form = check_document_form(
            before,
            forms,
            expectations=expected,
            rdf_direction=rdf_direction,
            budget=selected,
        )
        before_source = before_form.source
        children.append(before_form.report)
        checked += 1
        retain_report(before_form.report, selected, issues)
        after = prepare_usages(moved, base=new, document_loader=loader, budget=selected)
        after_form = check_document_form(
            after,
            forms,
            expectations=expected,
            rdf_direction=rdf_direction,
            budget=selected,
        )
        after_source = after_form.source
        children.append(after_form.report)
        checked += 1
        retain_report(after_form.report, selected, issues)
        if not loader.unscoped():
            raise _Unsupported(
                "Supplied scoped or aliased context relocation is unqualified"
            )
        if expected:
            usage_report = _observations(before, after, expected, selected)
            children.append(usage_report)
            checked += 1
            retain_report(usage_report, selected, issues)
        if (
            before_source is not None
            and after_source is not None
            and before_form.report.verified
            and after_form.report.verified
        ):
            rdf_report = compare_rdf(
                before_source,
                after_source,
                budget=selected,
                timeout_seconds=timeout_seconds,
            )
            children.append(rdf_report)
            checked += 1
            retain_report(rdf_report, selected, issues)
        else:
            raise _Unsupported(
                "Both raw source/form checks must establish the required scope"
            )
        complete = checked == requested and all(child.complete for child in children)
        if complete and all(child.verified for child in children):
            output = serialized
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Document relocation exceeded its operation allowance")
    except (InputError, ResourceError) as error:
        try:
            unknown = isinstance(error, (_Unsupported, ResourceError))
            issues.append(
                Issue(
                    "documents.relocation_unverified"
                    if unknown
                    else "documents.relocation_invalid",
                    "Document relocation requires bijux-ontology[jsonld], supported "
                    "contexts/resources and all selected preservation obligations",
                    kind="unverified" if unknown else "input_error",
                    details=Location.from_error(error),
                )
            )
        except LimitExceeded:
            issues.exhausted("Document relocation diagnostic allowance exhausted")
    report = Report(
        "documents.relocation",
        checked,
        tuple(issues),
        requested=requested,
        complete=complete,
        children=tuple(children),
        regime="jsonld-pin-old-base",
    )
    return DocumentRelocation._from_owned(
        old_url=old,
        new_url=new,
        document_json=output if report.verified else None,
        report=report,
        before_form=before_form,
        after_form=after_form,
        before_source=before_source,
        after_source=after_source,
        usage_report=usage_report,
        rdf_report=rdf_report,
    )


def export_relocation(relocated: DocumentRelocation) -> RDFExport:
    """Bind accepted captured JSON bytes to the ordinary checked write protocol."""
    if not isinstance(relocated, DocumentRelocation):
        raise InputError("Expected an owned DocumentRelocation result")
    if not relocated.report.verified or relocated.document_json is None:
        return RDFExport(None, relocated.report, "json-ld")
    return RDFExport(
        relocated.document_json.encode("utf-8"), relocated.report, "json-ld"
    )
