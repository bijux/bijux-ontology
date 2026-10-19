"""Selected publisher promises layered over valid ordinary context processing."""

from dataclasses import dataclass

from bijux_ontology.absence import ABSENT
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import (
    Budget,
    IssueCollector,
    Limits,
    bound_text,
    retain_report,
)
from bijux_ontology.jsonld.checking import check_context
from bijux_ontology.jsonld.composition.checking import _Case, _case, _document
from bijux_ontology.jsonld.contexts.processing import (
    _capture,
    _dump,
    _operation,
    _own,
    _preflight,
    inspect_context,
)
from bijux_ontology.jsonld.processing.resources import (
    ContextResources,
    DocumentLoader,
    context_resources,
)
from bijux_ontology.jsonld.processing.runtime import processor
from bijux_ontology.rdf.identifiers import absolute_iri

from .probes import _Probe, _term, run_probe
from .types import (
    ContextOverride,
    ContextOverrideResult,
    ContextPublication,
    PublicationContract,
    PublishedTerm,
)


@dataclass(frozen=True)
class _Contract:
    """Operation-owned qualified policy and immutable representative inputs."""

    terms: tuple[PublishedTerm, ...]
    cases: tuple[_Case, ...]
    probes: tuple[_Probe, ...]
    version: bool
    vocabulary: object
    effective_base: object


def _capture_contract(contract: PublicationContract, budget: Budget) -> _Contract:
    """Capture all policy, samples and output slots before any loader call."""
    if not isinstance(contract, PublicationContract):
        raise InputError("Expected an explicit PublicationContract")
    terms = _capture(contract.terms, budget)
    cases = tuple(_case(case, budget) for case in _capture(contract.cases, budget))
    selected = {}
    for selected_term in terms:
        if (
            not isinstance(selected_term, PublishedTerm)
            or type(selected_term.protected) is not bool
        ):
            raise InputError("Published terms require explicit boolean protection")
        bound_text(selected_term.term, budget)
        name = str(selected_term.term)
        if not name or name.startswith("@") or name in selected:
            raise InputError("Select distinct nonkeyword publication term names")
        selected[name] = PublishedTerm(name, selected_term.protected)
    if not terms or not cases or len({case.name for case in cases}) != len(cases):
        raise InputError("Publication needs selected terms and distinct retained cases")
    if contract.required_version is not None:
        bound_text(contract.required_version, budget)
        if contract.required_version != "1.1":
            raise InputError(
                "The local declaration profile supports required version 1.1"
            )
    defaults = []
    for value in (contract.vocabulary, contract.effective_base):
        if value is not ABSENT and value is not None:
            bound_text(value, budget)
            absolute_iri(value)
            value = str(value)
        defaults.append(value)
    probes: list[_Probe] = []
    names: set[str] = set()
    covered: set[str] = set()
    for probe in _capture(contract.overrides, budget):
        if not isinstance(probe, ContextOverride):
            raise InputError("Expected typed ContextOverride scenarios")
        for value in (probe.name, probe.term, probe.expect):
            bound_text(value, budget)
        name, term = str(probe.name), str(probe.term)
        if not name or name in names or term not in selected:
            raise InputError("Probe names must be distinct and select published terms")
        expected = "blocked" if selected[term].protected else "allowed"
        if probe.expect != expected:
            raise InputError("Probe intent must match the selected protection promise")
        names.add(name)
        covered.add(term)
        definition = _own(probe.definition, budget)
        extension = tuple(_case(case, budget) for case in _capture(probe.cases, budget))
        if len({case.name for case in extension}) != len(extension):
            raise InputError("Extension case names must be distinct per probe")
        if expected == "allowed" and not any(
            expectation.path == (term,)
            for case in extension
            for expectation in case.expectations
        ):
            raise InputError("Permitted probes need an actual root-term extension case")
        if expected == "blocked" and extension:
            raise InputError(
                "Blocked probes cannot supply an executable extension sample"
            )
        count = len(cases) + len(extension) if expected == "allowed" else 0
        # These slots and their final immutable public reference tuples are paid
        # before actual processing, including the exhaustion-return path.
        budget.consume("work", count + 1)
        budget.consume("states", count + 3)
        budget.consume("bytes", 192 + 24 * count)
        probes.append(
            _Probe(name, term, definition, expected, extension, [None] * count)
        )
    if covered != set(selected):
        raise InputError(
            "Every selected published term requires an actual override probe"
        )
    budget.consume("states", len(cases))
    budget.consume("bytes", 64 + 16 * len(cases))
    return _Contract(
        tuple(selected.values()),
        cases,
        tuple(probes),
        contract.required_version is not None,
        *defaults,
    )


def check_context_publication(
    context: object,
    contract: PublicationContract,
    *,
    base: str | None = None,
    document_loader: DocumentLoader | None = None,
    limits: Limits | None = None,
    budget: Budget | None = None,
) -> ContextPublication:
    """Check opt-in root protection/default/declaration promises and real probes.

    Ordinary JSON-LD validation imposes none of these policies. A declaration
    requirement means an authored local root @version, never implicit 1.1 mode.
    Arrays, remote contexts and imported declaration history remain unknown for
    that requirement. Selected scoped definitions need a richer scope contract
    and are unknown here. Override scenarios use maintained processing and
    preserve every retained case; the raw expected native rejection remains
    available separately from the policy result that verifies the guard.
    """
    operation = _operation(
        limits,
        budget
        if budget is not None
        else document_loader.budget
        if isinstance(document_loader, ContextResources)
        else None,
    )
    issues = IssueCollector(operation)
    inspection = None
    context_json = None
    captured = None
    case_reports: list[Report | None] = []
    children: list[Report] = []
    checked, requested = 0, 1
    complete = False
    try:
        if base is not None:
            bound_text(base, operation)
            absolute_iri(base)
        owned = _own(context, operation)
        context_json = _dump(owned, operation)
        captured = _capture_contract(contract, operation)
        case_reports = [None] * len(captured.cases)
        requested += len(captured.terms) + len(captured.cases) + len(captured.probes)
        requested += int(captured.version) + int(captured.vocabulary is not ABSENT)
        requested += int(captured.effective_base is not ABSENT)
        processor()
        if document_loader is None or isinstance(document_loader, ContextResources):
            resources = context_resources(document_loader, budget=operation)
        else:

            def qualified(url: str, options: dict[str, object]) -> dict[str, object]:
                """Bound callback data and errors before snapshots or diagnostics."""
                try:
                    response = document_loader(url, options)
                except InputError as error:
                    bound_text(str(error), operation)
                    raise
                _preflight(response, operation)
                return response

            resources = context_resources(qualified, budget=operation)
        inspection = inspect_context(
            owned, base=base, document_loader=resources, budget=operation
        )
        children.append(inspection.report)
        checked += 1
        retain_report(inspection.report, operation, issues)
        if inspection.report.verified and inspection.view is not None:
            originals = {}
            for selected in captured.terms:
                term = _term(inspection, selected.term, operation)
                if (
                    term is None
                    or term.iri is None
                    or term.scoped_context_json is not None
                ):
                    issues.append(
                        Issue(
                            "context.publication_unverified",
                            "Selected root definition or scope is not qualified",
                            selected.term,
                            kind="unverified",
                        )
                    )
                    continue
                originals[selected.term] = term
                checked += 1
                if term.protected != selected.protected:
                    issues.append(
                        Issue(
                            "context.publication_mismatch",
                            "Effective protection differs from the publication promise",
                            selected.term,
                            expected=str(selected.protected),
                            actual=str(term.protected),
                        )
                    )
            defaults = inspection.view.defaults
            for expected, actual, location in (
                (captured.vocabulary, defaults.vocabulary, "@vocab"),
                (captured.effective_base, defaults.effective_base, "@base"),
            ):
                if expected is not ABSENT:
                    checked += 1
                    if expected != actual:
                        issues.append(
                            Issue(
                                "context.publication_mismatch",
                                "Effective context default differs from its promise",
                                location,
                                expected=str(expected),
                                actual=str(actual),
                            )
                        )
            if captured.version:
                if (
                    not isinstance(owned, dict)
                    or "@context" in owned
                    or "@import" in owned
                    and "@version" not in owned
                ):
                    issues.append(
                        Issue(
                            "context.publication_unverified",
                            "Authored local root version declaration is not qualified",
                            kind="unverified",
                        )
                    )
                else:
                    checked += 1
                    if owned.get("@version") != 1.1:
                        issues.append(
                            Issue(
                                "context.publication_mismatch",
                                "Required authored local @version 1.1 is absent",
                                "@version",
                            )
                        )
            for index, case in enumerate(captured.cases):
                result = check_context(
                    _document(case, owned, operation),
                    case.expectations,
                    base=base,
                    document_loader=resources,
                    budget=operation,
                )
                case_reports[index] = result
                children.append(result)
                checked += 1
                retain_report(result, operation, issues)
            for probe in captured.probes:
                if probe.term not in originals:
                    continue
                probe.report = run_probe(
                    probe,
                    owned,
                    originals[probe.term],
                    captured.cases,
                    resources,
                    base,
                    operation,
                )
                children.append(probe.report)
                checked += 1
                retain_report(probe.report, operation, issues)
            complete = checked == requested and all(
                child.complete for child in children
            )
    except (LimitExceeded, MemoryError, RecursionError):
        issues.exhausted("Context publication checking exceeded its operation limits")
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "context.publication_unverified",
                    str(error),
                    kind="unverified"
                    if isinstance(error, ResourceError)
                    else "input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Context publication diagnostic allowance exhausted")
    return ContextPublication(
        context_json,
        inspection,
        tuple(report for report in case_reports if report is not None),
        tuple(
            ContextOverrideResult(
                probe.name,
                probe.replacement,
                probe.inspection,
                tuple(report for report in probe.cases if report is not None),
                probe.report,
            )
            for probe in captured.probes
        )
        if captured is not None
        else (),
        Report(
            "jsonld.publication",
            checked,
            tuple(issues),
            requested=requested,
            complete=complete,
            children=tuple(children),
            regime="selected-root-publication-contract",
        ),
    )
