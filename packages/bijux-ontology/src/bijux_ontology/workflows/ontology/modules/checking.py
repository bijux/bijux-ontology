"""Check finite explicit provider wiring without combining candidate theories."""

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.execution.budgets import Operation
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.limits import Budget, IssueCollector, Limits, bound_text
from bijux_ontology.owl.obligations import (
    ObligationResult,
    OntologyObligation,
    _NativeProofs,
)
from bijux_ontology.owl.properties import PropertyContract, property_contract
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.snapshot_metadata import SnapshotMetadata
from bijux_ontology.rdf.terms import bound_node
from bijux_ontology.reports import Issue, Report
from bijux_ontology.vocabulary.browsing.inspection import VocabularyView
from bijux_ontology.vocabulary.builtins import builtin_kinds, kind_matches
from bijux_ontology.vocabulary.sources.types import source_role

from .types import (
    ModuleBinding,
    ModuleContract,
    ModulePropertyRequirement,
    ModuleProvider,
    ModuleRequirement,
    ModuleRequirementResult,
    ModuleWiring,
)


def _reserve(budget: Budget, count: int = 1) -> None:
    """Prepay mutable and terminal immutable evidence references together."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 32 * count)


def _text(value: str, budget: Budget, *, iri: bool = False) -> None:
    """Bound caller text before hashing, stripping, parsing or diagnostic use."""
    if type(value) is not str:
        raise InputError("Module contracts require plain text names and IRIs")
    bound_text(value, budget)
    if iri:
        absolute_iri(value)
    elif not value.strip():
        raise InputError("Module names must be nonempty")


def _exports(
    values: Sequence[TermDeclaration], budget: Budget
) -> tuple[TermDeclaration, ...]:
    """Capture named public roles without inferring exports from private facts."""
    result = capture_items(values, TermDeclaration, budget)
    seen = set()
    for value in result:
        _text(value.iri, budget, iri=True)
        bound_text(value.location, budget)
        kinds = capture_items(value.kinds, str, budget)
        for kind in kinds:
            _text(kind, budget, iri=True)
        if not kinds or value.iri in seen:
            raise InputError("Public exports need explicit roles and unique names")
        _reserve(budget)
        seen.add(value.iri)
    return result


def _public(iri: str, exports: set[str], budget: Budget) -> None:
    """Require custom public operands before native reflexivity can prove them."""
    _text(iri, budget, iri=True)
    if iri not in exports and not builtin_kinds(iri):
        raise InputError("Required custom operands must be explicit public exports")


def _requirement(value: ModuleRequirement, budget: Budget) -> ModuleRequirement:
    """Own one finite selected signature and its typed native questions."""
    _text(value.name, budget)
    exports = _exports(value.exports, budget)
    if not exports:
        raise InputError("A required module signature needs named public exports")
    _reserve(budget, len(exports))
    names = {term.iri for term in exports}
    questions = capture_items(value.obligations, OntologyObligation, budget)
    for question in questions:
        bound_text(question.location, budget)
        for iri in capture_items(question.actual, str, budget):
            _public(iri, names, budget)
        if question.required is not None:
            _public(question.required, names, budget)
    properties = capture_items(value.properties, ModulePropertyRequirement, budget)
    for property_question in properties:
        _public(property_question.property, names, budget)
        _public(property_question.required, names, budget)
        bound_text(property_question.location, budget)
        _text(property_question.position, budget)
        _text(property_question.relation, budget)
        if property_question.position not in (
            "domain",
            "range",
        ) or property_question.relation not in (
            "subsumption",
            "datatype_inclusion",
        ):
            raise InputError("Select a supported property endpoint relation")
    return ModuleRequirement(value.name, exports, questions, properties)


@dataclass
class _Attempt:
    """Retain each native observation before a later allocation can exhaust."""

    requirement: ModuleRequirement
    bindings: tuple[ModuleBinding, ...]
    providers: tuple[ModuleProvider, ...]
    issues: list[Issue] = field(default_factory=list)
    proofs: list[ObligationResult] = field(default_factory=list)
    properties: list[tuple[ModulePropertyRequirement, PropertyContract]] = field(
        default_factory=list
    )
    finished: bool = False

    def freeze(self) -> ModuleRequirementResult:
        """Use prepaid terminal references, including after a genuine cutoff."""
        return ModuleRequirementResult(
            self.requirement,
            self.bindings,
            self.providers,
            tuple(self.proofs),
            tuple(self.properties),
            Report(
                "ontology.module_requirement",
                int(self.finished),
                tuple(self.issues),
                allow_empty=True,
                requested=1,
                complete=self.finished,
                regime="one-level-selected-module-signature",
            ),
        )


def _issue(attempt: _Attempt, issues: IssueCollector, issue: Issue) -> None:
    """Keep the same paid diagnostic in raw and per-requirement evidence."""
    _reserve(issues.budget)
    issues.append(issue)
    attempt.issues.append(issue)


def _unknown(
    attempt: _Attempt, issues: IssueCollector, message: str, location: str = ""
) -> None:
    """An unresolved finite obligation is independent of unrelated native truth."""
    _issue(
        attempt,
        issues,
        Issue(
            "ontology.module_obligation_unverified",
            message,
            location or attempt.requirement.name,
            kind="unverified",
        ),
    )


def _provider(provider: ModuleProvider, budget: Budget) -> Graph:
    """Adopt exact already-selected assertions without a new source copy/index."""
    view = provider.view
    for scope, graph in view._reader.scopes:
        bound_text(scope, budget)
        if source_role(scope) == "lookup":
            raise ResourceError("Lookup documentation cannot supply a module provider")
        budget.consume("resources")
        for triple in graph:
            budget.consume("work")
            budget.consume("triples")
            for node in triple:
                bound_node(node, budget)
    selected_scopes = {scope for scope, _ in view._reader.scopes}
    for scope, handle in view._vocabulary._source_handles:
        budget.consume("work")
        if scope in selected_scopes:
            metadata = SnapshotMetadata.capture(handle.prepared, budget)
            if metadata.graph_fidelity != "complete":
                raise ResourceError("Provider selected graph coverage is incomplete")
    return view._read()


def _symbols(
    attempt: _Attempt, provider: ModuleProvider, issues: IssueCollector
) -> bool:
    """Native exact declarations and public inventory are separate obligations."""
    budget = issues.budget
    exports = _exports(provider.contract.exports, budget)
    _reserve(budget, len(exports))
    public = {term.iri: term for term in exports}
    valid = True
    for term in attempt.requirement.exports:
        budget.consume("work")
        info = provider.view.lookup(term.iri)
        actual = frozenset(info.kinds) | builtin_kinds(term.iri)
        declaration = public.get(term.iri)
        if declaration is None or not actual:
            valid = False
            _issue(
                attempt,
                issues,
                Issue(
                    "ontology.module_export_missing",
                    "Required symbol is not publicly exported and declared "
                    "by this provider",
                    term.location or term.iri,
                    actual=term.iri,
                ),
            )
        elif not kind_matches(actual, term.kinds) or not kind_matches(
            actual, declaration.kinds
        ):
            valid = False
            _issue(
                attempt,
                issues,
                Issue(
                    "ontology.module_kind_mismatch",
                    "Public symbol lacks its required or promised "
                    "native declaration role",
                    term.location or term.iri,
                    actual=term.iri,
                ),
            )
    return valid


def _role(
    view: VocabularyView, iri: str, kinds: tuple[str, ...], budget: Budget
) -> bool:
    """Gate native proof operands with existing role logic, including builtins."""
    bound_text(iri, budget)
    actual = frozenset(view.lookup(iri).kinds) | builtin_kinds(iri)
    return bool(actual) and kind_matches(actual, kinds)


def _answer(
    attempt: _Attempt,
    native: _NativeProofs,
    question: OntologyObligation,
    issues: IssueCollector,
) -> None:
    """Retain the existing engine answer without broadening its claim."""
    _reserve(issues.budget)
    answer = native.answer(question)
    attempt.proofs.append(answer)
    if answer.status == "unknown":
        _unknown(
            attempt,
            issues,
            "The selected native relationship is not established",
            question.location,
        )
    elif answer.status == "contradicted":
        _issue(
            attempt,
            issues,
            Issue(
                "ontology.module_obligation_contradicted",
                "The selected independent semantic obligation is contradicted",
                question.location or attempt.requirement.name,
                actual=question.actual[0] if question.actual else None,
                expected=question.required,
            ),
        )


def _proofs(
    attempt: _Attempt,
    provider: ModuleProvider,
    native: _NativeProofs,
    issues: IssueCollector,
) -> None:
    """Discharge only role-qualified selected questions over this provider alone."""
    budget = issues.budget
    for question in attempt.requirement.obligations:
        kind = (
            (str(RDFS.Datatype),)
            if question.kind == "datatype_inclusion"
            else (str(OWL.Class), str(RDFS.Class))
        )
        operands = (
            *question.actual,
            *((question.required,) if question.required else ()),
        )
        if any(not _role(provider.view, iri, kind, budget) for iri in operands):
            _unknown(
                attempt,
                issues,
                "Required proof operand lacks its selected native role",
                question.location,
            )
            continue
        _answer(attempt, native, question, issues)
    for property_question in attempt.requirement.properties:
        required_kind = (
            (str(RDFS.Datatype),)
            if property_question.relation == "datatype_inclusion"
            else (str(OWL.Class), str(RDFS.Class))
        )
        if not _role(
            provider.view, property_question.property, (str(RDF.Property),), budget
        ) or not _role(
            provider.view, property_question.required, required_kind, budget
        ):
            _unknown(
                attempt,
                issues,
                "Required property endpoint lacks its selected native role",
                property_question.location,
            )
            continue
        _reserve(budget)
        contract = property_contract(
            native.graph, property_question.property, budget=budget
        )
        attempt.properties.append((property_question, contract))
        endpoints = (
            contract.parents
            if property_question.position == "domain"
            else contract.values
        )
        established = False
        for endpoint in endpoints:
            budget.consume("work")
            if not isinstance(endpoint, URIRef) or not _role(
                provider.view, str(endpoint), required_kind, budget
            ):
                continue
            _reserve(budget)
            result = native.answer(
                OntologyObligation(
                    property_question.relation,
                    (str(endpoint),),
                    property_question.required,
                    property_question.location,
                )
            )
            attempt.proofs.append(result)
            established |= result.status == "established"
        if not established:
            _unknown(
                attempt,
                issues,
                "The selected effective property endpoint guarantee is not established",
                property_question.location,
            )


def check_module_wiring(
    consumer: ModuleContract,
    providers: Sequence[ModuleProvider],
    bindings: Sequence[ModuleBinding],
    *,
    limits: Limits | None = None,
    budget: Budget | None = None,
    timeout: float = 30,
    cancel: threading.Event | None = None,
) -> ModuleWiring:
    """Check one explicit finite consumer interface with independently bound views.

    Provider private dependencies are not recursively checked. No lookup graph,
    unbound candidate, inverse substitution or whole-provider equivalence is
    inferred. Property endpoint guarantees are conjunctions; missing evidence
    stays unknown rather than manufacturing OWL inconsistency or non-entailment.
    Existing asserted proof engines run without Java and share one host budget.
    """
    operation = Operation(limits, budget, timeout, cancel)
    issues = IssueCollector(operation)
    attempts: list[_Attempt] = []
    checked = requested = 0
    finished = False
    try:
        if not isinstance(consumer, ModuleContract):
            raise InputError("Expected an explicit consumer module contract")
        _text(consumer.name, operation)
        _exports(consumer.exports, operation)
        requirements = capture_items(consumer.requires, ModuleRequirement, operation)
        selected = tuple(_requirement(value, operation) for value in requirements)
        _reserve(operation, len(selected))
        names = {value.name for value in selected}
        if len(names) != len(selected):
            raise InputError("Consumer requirement names must be unique")
        requested = len(selected)
        supplied = capture_items(providers, ModuleProvider, operation)
        available = {}
        for provider in supplied:
            if not isinstance(provider.contract, ModuleContract) or not isinstance(
                provider.view, VocabularyView
            ):
                raise InputError(
                    "Providers need actual contracts and selected owned views"
                )
            _text(provider.contract.name, operation)
            if provider.contract.name in available:
                raise InputError("Provider names must be unique")
            _reserve(operation)
            available[provider.contract.name] = provider
        wired = capture_items(bindings, ModuleBinding, operation)
        for binding in wired:
            _text(binding.requirement, operation)
            _text(binding.provider, operation)
            if binding.requirement not in names:
                raise InputError("Binding names an undeclared consumer requirement")
        engines: dict[int, _NativeProofs] = {}
        for requirement in selected:
            _reserve(operation, 2 * len(wired) + 1)
            matched = []
            for value in wired:
                operation.consume(
                    "work", len(value.requirement) + len(requirement.name) + 1
                )
                if value.requirement == requirement.name:
                    matched.append(value)
            matches = tuple(matched)
            operation.consume("work", sum(len(value.provider) + 1 for value in matches))
            bound = tuple(
                available[value.provider]
                for value in matches
                if value.provider in available
            )
            attempt = _Attempt(requirement, matches, bound)
            attempts.append(attempt)
            if len(matches) != 1 or len(bound) != 1:
                _unknown(
                    attempt, issues, "Required provider is unresolved or multiply bound"
                )
            else:
                provider = bound[0]
                try:
                    key = id(provider)
                    if key not in engines:
                        graph = _provider(provider, operation)
                        _reserve(operation)
                        engines[key] = _NativeProofs(graph, operation)
                    _symbols(attempt, provider, issues)
                    _proofs(attempt, provider, engines[key], issues)
                except ResourceError as error:
                    _unknown(attempt, issues, str(error))
            attempt.finished = True
            checked += 1
        finished = True
    except LimitExceeded as error:
        issues.exhausted(str(error), location="ontology.module_wiring")
    except InputError as error:
        try:
            issues.append(
                Issue(
                    "ontology.module_input_error",
                    str(error),
                    error.location,
                    kind="input_error",
                )
            )
        except LimitExceeded:
            issues.exhausted("Stopped retaining module contract input failure")
    results = tuple(attempt.freeze() for attempt in attempts)
    report = Report(
        "ontology.module_wiring",
        checked,
        tuple(issues),
        allow_empty=finished and not requested,
        requested=max(requested, checked + int(not finished)),
        complete=finished,
        children=tuple(item.report for item in results),
        regime="one-level-selected-module-signatures",
    )
    return ModuleWiring(consumer, results, report)
