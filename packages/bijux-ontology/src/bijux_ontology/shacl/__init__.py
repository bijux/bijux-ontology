"""Optional supplied-shapes validation with preserved backend evidence."""

from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.checking import ShaclFinding, ShaclResult, validate_shapes
from bijux_ontology.shacl.coverage import (
    ShaclCoverage,
    ShaclCoverageContract,
    ShaclTarget,
)
from bijux_ontology.shacl.findings import ShaclSource
from bijux_ontology.shacl.functions import (
    ShaclFunctionCatalog,
    ShaclFunctionParameter,
    ShaclSparqlFunction,
)
from bijux_ontology.shacl.paths import ShaclPath

__all__ = [
    "ShaclConstraintRequirement",
    "ShaclCoverage",
    "ShaclCoverageContract",
    "ShaclFinding",
    "ShaclFunctionCatalog",
    "ShaclFunctionParameter",
    "ShaclResult",
    "ShaclSource",
    "ShaclSparqlFunction",
    "ShaclPath",
    "ShaclTarget",
    "validate_shapes",
]
