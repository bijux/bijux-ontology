"""Inspect selected typed shapes and explicitly edit ordinary count bounds."""

from .editing import edit_shape_counts
from .reading import inspect_shapes
from .types import ShapeCountEdit, ShapeEdit, ShapeInspection, ShapeRemainder

__all__ = [
    "ShapeCountEdit",
    "ShapeEdit",
    "ShapeInspection",
    "ShapeRemainder",
    "edit_shape_counts",
    "inspect_shapes",
]
