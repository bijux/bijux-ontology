"""Representative shape-release impact with raw validation and target evidence."""

from .comparison import assess_shape_upgrade
from .types import (
    ShapeCorrespondence,
    ShapeFindingChange,
    ShapeSample,
    ShapeSampleAssessment,
    ShapeTargetChange,
    ShapeUpgradeAssessment,
)

__all__ = [
    "ShapeCorrespondence",
    "ShapeFindingChange",
    "ShapeSample",
    "ShapeSampleAssessment",
    "ShapeTargetChange",
    "ShapeUpgradeAssessment",
    "assess_shape_upgrade",
]
