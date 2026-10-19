"""Checked source-aware module moves and explicit inlining, without publication."""

from .checking import prepare_module_refactor
from .types import (
    AxiomMove,
    ChangedModule,
    ConsumerModuleCheck,
    ModuleConsumer,
    ModuleImportChange,
    ModuleInline,
    ModuleInterfaceChange,
    ModuleRefactorCandidate,
    ModuleSet,
    NamespaceTransfer,
)

__all__ = [
    "AxiomMove",
    "ChangedModule",
    "ConsumerModuleCheck",
    "ModuleConsumer",
    "ModuleImportChange",
    "ModuleInline",
    "ModuleInterfaceChange",
    "ModuleRefactorCandidate",
    "ModuleSet",
    "NamespaceTransfer",
    "prepare_module_refactor",
]
