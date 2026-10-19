"""Finite maintained ROBOT table authoring with checked typed native output."""

from .backend import prepare_template_backend
from .checking import compile_table_template
from .types import (
    TableCompilation,
    TableRowReceipt,
    TemplateBackend,
    TemplateColumn,
    TemplateIdentifier,
    TemplateTerm,
)

__all__ = [
    "TableCompilation",
    "TableRowReceipt",
    "TemplateBackend",
    "TemplateColumn",
    "TemplateIdentifier",
    "TemplateTerm",
    "compile_table_template",
    "prepare_template_backend",
]
