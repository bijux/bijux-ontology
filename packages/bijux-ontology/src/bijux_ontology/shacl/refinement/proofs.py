"""Sufficient implication rules for already qualified finite conjunctions."""

from rdflib import BNode, Literal
from rdflib.term import Node

from bijux_ontology.execution.limits import Budget
from bijux_ontology.rdf.literals import check_literal
from bijux_ontology.rdf.terms import literal_datatype, term_key
from bijux_ontology.shacl.refinement.normalization import Branch, Obligation, path_key
from bijux_ontology.shacl.refinement.types import RefinementProof


def _identities(values: tuple[Node, ...], budget: Budget) -> set[tuple[object, ...]]:
    """Charge exact term comparisons before retaining the finite identity set."""
    result: set[tuple[object, ...]] = set()
    for value in values:
        budget.consume("work")
        budget.consume("states")
        result.add(term_key(value))
    return result


def _maximum(item: Obligation) -> int | None:
    """An exact allowed set also bounds the number of distinct RDF values."""
    count = None if item.allowed is None else len(item.allowed)
    return (
        count
        if item.maximum is None
        else item.maximum
        if count is None
        else min(count, item.maximum)
    )


def _value_rules(child: Obligation, parent: Obligation, budget: Budget) -> bool:
    """Prove only exact datatype/node-kind restrictions or qualified finite sets."""
    if _maximum(child) == 0:
        return True
    if not child.kinds <= parent.kinds:
        if child.allowed is None:
            return False
        for value in child.allowed:
            budget.consume("work")
            kind = (
                "literal"
                if isinstance(value, Literal)
                else "blank"
                if isinstance(value, BNode)
                else "iri"
            )
            if kind not in parent.kinds:
                return False
    for datatype in parent.datatypes:
        budget.consume("work", len(child.datatypes) + 1)
        if datatype in child.datatypes:
            continue
        if child.allowed is None:
            return False
        for value in child.allowed:
            budget.consume("work")
            if (
                not isinstance(value, Literal)
                or str(literal_datatype(value)) != datatype
            ):
                return False
            if not check_literal(value, budget=budget).verified:
                return False
    return True


def _implies(child: Obligation, parent: Obligation, budget: Budget) -> bool:
    """Compare necessary conjunction bounds without inferring satisfiability."""
    budget.consume("work")
    if child.minimum < parent.minimum:
        return False
    maximum = _maximum(child)
    if parent.maximum is not None and (maximum is None or maximum > parent.maximum):
        return False
    required = _identities(child.fixed, budget)
    if child.allowed is not None and child.minimum >= len(child.allowed):
        required.update(_identities(child.allowed, budget))
    if not _identities(parent.fixed, budget) <= required:
        return False
    if (
        parent.allowed is not None
        and maximum != 0
        and (
            child.allowed is None
            or not _identities(child.allowed, budget)
            <= _identities(parent.allowed, budget)
        )
    ):
        return False
    return _value_rules(child, parent, budget)


def prove(
    parent: tuple[Branch, ...],
    child: tuple[Branch, ...],
    budget: Budget,
) -> tuple[RefinementProof, ...]:
    """Every child conjunction must entail at least one complete parent branch."""
    proofs = []
    for child_index, branch in enumerate(child):
        budget.consume("states", len(branch))
        available = {path_key(item.path, budget): item for item in branch}
        for parent_index, required in enumerate(parent):
            budget.consume("work")
            if all(
                _implies(
                    available.get(path_key(item.path, budget), Obligation(item.path)),
                    item,
                    budget,
                )
                for item in required
            ):
                budget.consume("states", 1 + len(required))
                proofs.append(
                    RefinementProof(
                        child_index, parent_index, tuple(item.path for item in required)
                    )
                )
                break
        else:
            return tuple(proofs)
    return tuple(proofs)
