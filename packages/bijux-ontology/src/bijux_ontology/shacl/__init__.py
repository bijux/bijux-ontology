"""Optional supplied-shapes validation with preserved backend evidence."""

from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.checking import ShaclFinding, ShaclResult, validate_shapes
from bijux_ontology.shacl.coverage import (
    ShaclCoverage,
    ShaclCoverageContract,
    ShaclTarget,
)

__all__ = [
    "ShaclConstraintRequirement",
    "ShaclCoverage",
    "ShaclCoverageContract",
    "ShaclFinding",
    "ShaclResult",
    "ShaclTarget",
    "validate_shapes",
]
