"""Concrete maintained materialization programs and accepted revision receipts."""

from dataclasses import dataclass
from typing import Literal, TypeAlias

from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.entailment.types import EntailmentRegime, EntailmentResult
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.shacl.functions import ShaclFunctionCatalog
from bijux_ontology.shacl.rules.types import RuleMaterialization


@dataclass(frozen=True)
class EntailmentPlan:
    """One explicit maintained RDFS or OWL-RL complete recomputation."""

    regime: EntailmentRegime

    def __post_init__(self) -> None:
        """Reject fallback to a different entailment regime."""
        if self.regime not in ("rdfs", "owl-rl"):
            raise InputError("Select the rdfs or owl-rl materialization regime")


@dataclass(frozen=True)
class RulePlan:
    """A trusted finite SHACL program and its actual required output contracts.

    Sessions always request global-fixed-point mode. Callback contracts remain
    trusted in-process Python, as in the ordinary materialize_rules operation.
    """

    rules: PreparedSource
    use_shapes: tuple[str | URIRef, ...]
    checks: tuple[MigrationCheck, ...]
    trusted: bool = False
    max_passes: int = 100
    functions: ShaclFunctionCatalog | None = None

    def __post_init__(self) -> None:
        """Require concrete immutable program inventories without executing them."""
        if not isinstance(self.rules, PreparedSource):
            raise InputError("Rule plans require a PreparedSource")
        if type(self.use_shapes) is not tuple or type(self.checks) is not tuple:
            raise InputError("Rule selections and contracts require immutable tuples")
        if type(self.trusted) is not bool:
            raise InputError("Rule execution requires explicit boolean trust")
        if type(self.max_passes) is not int or self.max_passes <= 0:
            raise InputError("Rule pass bounds require a positive integer")
        if self.functions is not None and not isinstance(
            self.functions, ShaclFunctionCatalog
        ):
            raise InputError("Rule functions require a ShaclFunctionCatalog")


MaterializationPlan: TypeAlias = EntailmentPlan | RulePlan
NativeMaterialization: TypeAlias = EntailmentResult | RuleMaterialization


@dataclass(frozen=True, init=False, eq=False)
class MaterializationRevision:
    """An opaque accepted session revision, matched by actual receipt identity."""

    number: int

    def __init__(self) -> None:
        raise InputError("Materialization revisions are issued by owned sessions")

    @classmethod
    def _owned(cls, number: int) -> "MaterializationRevision":
        """Mint only after complete candidate and atomic publication qualification."""
        result = object.__new__(cls)
        object.__setattr__(result, "number", number)
        return result


@dataclass(frozen=True)
class MaterializationUpdate:
    """A newly published closure or an honest failed update retaining old state."""

    revision: MaterializationRevision | None
    previous: MaterializationRevision | None
    materialization: NativeMaterialization | None
    report: Report
    strategy: Literal["complete-recomputation"] = "complete-recomputation"
