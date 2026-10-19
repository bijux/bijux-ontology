"""Bounded authored interface qualification, without workflow or provider policy."""

from collections.abc import Sequence
from dataclasses import replace

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.inputs import capture_items
from bijux_ontology.execution.limits import Budget, bound_text
from bijux_ontology.owl.encoding.roles import builtin_kinds
from bijux_ontology.owl.obligations import OntologyObligation
from bijux_ontology.rdf.identifiers import absolute_iri

from .contracts import ModuleContract, ModulePropertyRequirement, ModuleRequirement


def _reserve(budget: Budget, count: int = 1) -> None:
    """Prepay mutable and terminal immutable evidence references together."""
    budget.consume("states", count)
    budget.consume("bytes", 64 + 32 * count)


def contract_text(value: str, budget: Budget, *, iri: bool = False) -> None:
    """Bound caller text before hashing, stripping, parsing or diagnostic use."""
    if type(value) is not str:
        raise InputError("Module contracts require plain text names and IRIs")
    bound_text(value, budget)
    if iri:
        absolute_iri(value)
    elif not value.strip():
        raise InputError("Module names must be nonempty")


def capture_exports(
    values: Sequence[TermDeclaration], budget: Budget
) -> tuple[TermDeclaration, ...]:
    """Capture named public roles without inferring exports from private facts."""
    result = capture_items(values, TermDeclaration, budget)
    seen = set()
    for value in result:
        contract_text(value.iri, budget, iri=True)
        bound_text(value.location, budget)
        kinds = capture_items(value.kinds, str, budget)
        for kind in kinds:
            contract_text(kind, budget, iri=True)
        if not kinds or value.iri in seen:
            raise InputError("Public exports need explicit roles and unique names")
        _reserve(budget)
        seen.add(value.iri)
    return result


def _public(iri: str, exports: set[str], budget: Budget) -> None:
    """Require custom public operands before native reflexivity can prove them."""
    contract_text(iri, budget, iri=True)
    if iri not in exports and not builtin_kinds(iri):
        raise InputError("Required custom operands must be explicit public exports")


def capture_requirement(value: ModuleRequirement, budget: Budget) -> ModuleRequirement:
    """Own one finite selected signature and its typed native questions."""
    contract_text(value.name, budget)
    exports = capture_exports(value.exports, budget)
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
        contract_text(property_question.position, budget)
        contract_text(property_question.relation, budget)
        if property_question.position not in (
            "domain",
            "range",
        ) or property_question.relation not in (
            "subsumption",
            "datatype_inclusion",
        ):
            raise InputError("Select a supported property endpoint relation")
    return ModuleRequirement(value.name, exports, questions, properties)


def capture_contract(value: ModuleContract, budget: Budget) -> ModuleContract:
    """Reuse finite module contract qualification and own terminal records."""
    if type(value) is not ModuleContract or type(value.name) is not str:
        raise InputError("Interfaces need ordinary explicit module contracts")
    bound_text(value.name, budget)
    if not value.name.strip():
        raise InputError("Module interface names must be nonempty")
    exports = capture_exports(value.exports, budget)
    requirements = capture_items(value.requires, ModuleRequirement, budget)
    budget.consume("bytes", 128 + 128 * (len(exports) + len(requirements)))
    owned_exports = []
    for term in exports:
        if type(term) is not TermDeclaration:
            raise InputError("Public exports require ordinary term declarations")
        owned_exports.append(
            TermDeclaration(term.iri, tuple(term.kinds), term.location)
        )
    owned_requirements = []
    for requirement in requirements:
        if type(requirement) is not ModuleRequirement:
            raise InputError("Module requirements need ordinary records")
        qualified = capture_requirement(requirement, budget)
        budget.consume(
            "states",
            len(qualified.exports)
            + len(qualified.obligations)
            + len(qualified.properties),
        )
        budget.consume(
            "bytes",
            128
            + 256
            * (
                len(qualified.exports)
                + len(qualified.obligations)
                + len(qualified.properties)
            ),
        )
        if (
            any(type(term) is not TermDeclaration for term in qualified.exports)
            or any(
                type(question) is not OntologyObligation
                for question in qualified.obligations
            )
            or any(
                type(question) is not ModulePropertyRequirement
                for question in qualified.properties
            )
        ):
            raise InputError("Module interfaces need ordinary nested contract records")
        for question in qualified.obligations:
            bound_text(question.kind, budget)
            bound_text(question.regime, budget)
        # Existing qualification pays every actual operand and selected relation.
        # Reconstructed frozen records exclude references to caller-owned arrays.
        owned_requirements.append(
            replace(
                qualified,
                exports=tuple(
                    TermDeclaration(t.iri, tuple(t.kinds), t.location)
                    for t in qualified.exports
                ),
                obligations=tuple(
                    replace(q, actual=tuple(q.actual)) for q in qualified.obligations
                ),
                properties=tuple(replace(q) for q in qualified.properties),
            )
        )
    return ModuleContract(value.name, tuple(owned_exports), tuple(owned_requirements))
