"""Finite sound assertion witnesses, distinct from a general consistency verdict."""

from .checking import check_assertion_witnesses
from .types import AssertionWitness, AssertionWitnesses, EqualityConsequence

__all__ = [
    "AssertionWitness",
    "AssertionWitnesses",
    "EqualityConsequence",
    "check_assertion_witnesses",
]
