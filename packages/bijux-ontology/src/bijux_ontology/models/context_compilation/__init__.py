"""Compile explicit output mappings and qualify finite generated inputs."""

from .compilation import compile_context
from .types import CompiledContext

__all__ = ["CompiledContext", "compile_context"]
