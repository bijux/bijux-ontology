"""Reviewable finite Python bindings and their generation evidence."""

from dataclasses import dataclass

from bijux_ontology.diagnostics.reports import Report


@dataclass(frozen=True)
class PythonBinding:
    """One explicit exported name, exact identifier and asserted declaration roles."""

    name: str
    iri: str
    kinds: tuple[str, ...]
    documentation: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedBindings:
    """Candidate text is inspectable; only its report establishes completeness."""

    source: str | None
    bindings: tuple[PythonBinding, ...]
    report: Report
