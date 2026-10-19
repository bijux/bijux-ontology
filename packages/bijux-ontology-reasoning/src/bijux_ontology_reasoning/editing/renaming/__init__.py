"""Checked OWL entity-role renaming with explicit module and annotation scope."""

from .checking import prepare_role_rename
from .types import (
    AnnotationRole,
    ModuleCheck,
    ModuleInterfaceChange,
    ModuleSet,
    RenameCandidate,
    RenamedModule,
    RoleRename,
)

__all__ = [
    "AnnotationRole",
    "ModuleCheck",
    "ModuleInterfaceChange",
    "ModuleSet",
    "RenameCandidate",
    "RenamedModule",
    "RoleRename",
    "prepare_role_rename",
]
