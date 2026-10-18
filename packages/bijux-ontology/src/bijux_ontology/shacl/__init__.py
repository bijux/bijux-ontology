"""Optional supplied-shapes validation with preserved backend evidence."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

_EXPORT_MODULES = {
    "LocalConflict": "bijux_ontology.shacl.analysis",
    "RootConflict": "bijux_ontology.shacl.analysis",
    "ShapeAnalysis": "bijux_ontology.shacl.analysis",
    "ShapeComponent": "bijux_ontology.shacl.analysis",
    "SignedDependency": "bijux_ontology.shacl.analysis",
    "analyze_shape_profile": "bijux_ontology.shacl.analysis",
    "ShaclComparison": "bijux_ontology.shacl.authoring",
    "ShaclConstraint": "bijux_ontology.shacl.authoring",
    "ShaclCount": "bijux_ontology.shacl.authoring",
    "ShaclLogical": "bijux_ontology.shacl.authoring",
    "ShaclNodeShape": "bijux_ontology.shacl.authoring",
    "ShaclParameter": "bijux_ontology.shacl.authoring.parameters",
    "ShaclPropertyShape": "bijux_ontology.shacl.authoring",
    "ShaclQualifiedCount": "bijux_ontology.shacl.authoring",
    "ShaclShapeRef": "bijux_ontology.shacl.authoring",
    "ShaclValue": "bijux_ontology.shacl.authoring",
    "compile_shapes": "bijux_ontology.shacl.authoring",
    "ShaclConstraintRequirement": "bijux_ontology.shacl.capabilities",
    "ShaclFinding": "bijux_ontology.shacl.findings",
    "ShaclResult": "bijux_ontology.shacl.execution.results",
    "validate_shapes": "bijux_ontology.shacl.checking",
    "ComponentCheck": "bijux_ontology.shacl.components",
    "ComponentShapes": "bijux_ontology.shacl.components",
    "ComponentUse": "bijux_ontology.shacl.components",
    "ComponentValidator": "bijux_ontology.shacl.components",
    "ShaclComponent": "bijux_ontology.shacl.components",
    "check_components": "bijux_ontology.shacl.components",
    "compile_components": "bijux_ontology.shacl.components",
    "ShaclCoverage": "bijux_ontology.shacl.coverage",
    "ShaclCoverageContract": "bijux_ontology.shacl.coverage",
    "ShaclTarget": "bijux_ontology.shacl.coverage",
    "ShaclSource": "bijux_ontology.shacl.findings",
    "ShaclFunctionCatalog": "bijux_ontology.shacl.functions",
    "ShaclFunctionParameter": "bijux_ontology.shacl.functions",
    "ShaclSparqlFunction": "bijux_ontology.shacl.functions",
    "ShapeCountEdit": "bijux_ontology.shacl.inspection",
    "ShapeEdit": "bijux_ontology.shacl.inspection",
    "ShapeInspection": "bijux_ontology.shacl.inspection",
    "ShapeRemainder": "bijux_ontology.shacl.inspection",
    "edit_shape_counts": "bijux_ontology.shacl.inspection",
    "inspect_shapes": "bijux_ontology.shacl.inspection",
    "ModelShapeBinding": "bijux_ontology.shacl.model_shapes",
    "ModelShapeTranslation": "bijux_ontology.shacl.model_shapes",
    "compile_model_shapes": "bijux_ontology.shacl.model_shapes",
    "PathFieldProjection": "bijux_ontology.shacl.path_fields",
    "project_path_field": "bijux_ontology.shacl.path_fields",
    "ShaclPath": "bijux_ontology.shacl.paths",
    "ProfileCase": "bijux_ontology.shacl.profiles",
    "ProfileComparison": "bijux_ontology.shacl.profiles",
    "ProfileExport": "bijux_ontology.shacl.profiles",
    "ShapeDependency": "bijux_ontology.shacl.profiles",
    "ShapeProfile": "bijux_ontology.shacl.profiles",
    "export_profile": "bijux_ontology.shacl.profiles",
    "extract_profile": "bijux_ontology.shacl.profiles",
    "ProfileRefinement": "bijux_ontology.shacl.refinement",
    "RefinementContract": "bijux_ontology.shacl.refinement",
    "RefinementProof": "bijux_ontology.shacl.refinement",
    "compare_shape_refinement": "bijux_ontology.shacl.refinement",
    "ShaclBatchResult": "bijux_ontology.shacl.session",
    "ShaclSession": "bijux_ontology.shacl.session",
}


def __getattr__(name: str) -> object:
    """Load the owning SHACL surface only when its public export is requested."""
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value: object = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose supported SHACL names without preparing unrelated validators."""
    return sorted(set(globals()) | set(__all__))
