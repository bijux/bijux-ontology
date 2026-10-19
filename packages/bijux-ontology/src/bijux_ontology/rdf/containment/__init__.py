"""Bounded RDF simple entailment with native existential witnesses."""

from .checking import check_simple_entailment
from .types import ContainmentCheck, TemplateBinding

__all__ = ["ContainmentCheck", "TemplateBinding", "check_simple_entailment"]
