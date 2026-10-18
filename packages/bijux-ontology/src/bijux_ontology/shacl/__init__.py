"""Optional supplied-shapes validation with preserved backend evidence."""

from bijux_ontology.shacl.analysis import (
    LocalConflict,
    RootConflict,
    ShapeAnalysis,
    ShapeComponent,
    SignedDependency,
    analyze_shape_profile,
)
from bijux_ontology.shacl.authoring import (
    ShaclComparison,
    ShaclConstraint,
    ShaclCount,
    ShaclLogical,
    ShaclNodeShape,
    ShaclParameter,
    ShaclPropertyShape,
    ShaclQualifiedCount,
    ShaclShapeRef,
    ShaclValue,
    compile_shapes,
)
from bijux_ontology.shacl.capabilities import ShaclConstraintRequirement
from bijux_ontology.shacl.checking import ShaclFinding, ShaclResult, validate_shapes
from bijux_ontology.shacl.components import (
    ComponentCheck,
    ComponentShapes,
    ComponentUse,
    ComponentValidator,
    ShaclComponent,
    check_components,
    compile_components,
)
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
from bijux_ontology.shacl.inspection import (
    ShapeCountEdit,
    ShapeEdit,
    ShapeInspection,
    ShapeRemainder,
    edit_shape_counts,
    inspect_shapes,
)
from bijux_ontology.shacl.model_shapes import (
    ModelShapeBinding,
    ModelShapeTranslation,
    compile_model_shapes,
)
from bijux_ontology.shacl.path_fields import PathFieldProjection, project_path_field
from bijux_ontology.shacl.paths import ShaclPath
from bijux_ontology.shacl.profiles import (
    ProfileCase,
    ProfileComparison,
    ProfileExport,
    ShapeDependency,
    ShapeProfile,
    export_profile,
    extract_profile,
)
from bijux_ontology.shacl.refinement import (
    ProfileRefinement,
    RefinementContract,
    RefinementProof,
    compare_shape_refinement,
)
from bijux_ontology.shacl.session import ShaclBatchResult, ShaclSession

__all__ = [
    "ProfileRefinement",
    "RefinementContract",
    "RefinementProof",
    "compare_shape_refinement",
    "ComponentCheck",
    "ComponentShapes",
    "ComponentUse",
    "ComponentValidator",
    "ShaclComponent",
    "ShaclParameter",
    "check_components",
    "compile_components",
    "LocalConflict",
    "RootConflict",
    "ShapeAnalysis",
    "ShapeComponent",
    "SignedDependency",
    "analyze_shape_profile",
    "ShapeCountEdit",
    "ShapeEdit",
    "ShapeInspection",
    "ShapeRemainder",
    "edit_shape_counts",
    "inspect_shapes",
    "ProfileCase",
    "ProfileComparison",
    "ProfileExport",
    "ShapeDependency",
    "ShapeProfile",
    "export_profile",
    "extract_profile",
    "PathFieldProjection",
    "project_path_field",
    "ModelShapeBinding",
    "ModelShapeTranslation",
    "compile_model_shapes",
    "ShaclComparison",
    "ShaclConstraint",
    "ShaclCount",
    "ShaclLogical",
    "ShaclNodeShape",
    "ShaclPropertyShape",
    "ShaclQualifiedCount",
    "ShaclShapeRef",
    "ShaclValue",
    "compile_shapes",
    "ShaclBatchResult",
    "ShaclConstraintRequirement",
    "ShaclCoverage",
    "ShaclCoverageContract",
    "ShaclFinding",
    "ShaclFunctionCatalog",
    "ShaclFunctionParameter",
    "ShaclResult",
    "ShaclSession",
    "ShaclSource",
    "ShaclSparqlFunction",
    "ShaclPath",
    "ShaclTarget",
    "validate_shapes",
]
