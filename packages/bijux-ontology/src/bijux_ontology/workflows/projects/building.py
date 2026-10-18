"""Typed file selections that build the ordinary validated project configuration."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self, TypeVar

from bijux_ontology._json import own_json
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.terms import bound_text
from bijux_ontology.workflows.projects.configuration import ProjectConfig
from bijux_ontology.workflows.upgrades.corpus import CorpusBranch

T = TypeVar("T")


def _capture(values: Sequence[T], budget: Budget) -> tuple[T, ...]:
    """Capture finite metadata with one count and bounded indexed access."""
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        raise InputError("Project builder inventories require finite sequences")
    count = len(values)
    budget.consume("states", count)
    budget.consume("work", count)
    try:
        return tuple(values[index] for index in range(count))
    except IndexError as error:
        raise InputError("Project builder inventory changed during capture") from error


def _filename(value: str | Path) -> str:
    """Preserve explicit local filenames, without accepting arbitrary coercion."""
    if not isinstance(value, str | Path):
        raise InputError("Project inputs require local filenames")
    return str(value)


@dataclass(frozen=True)
class CorpusDocumentInput:
    """Named actual old/new output files and one existing expectation file."""

    name: str
    before: str | Path
    expectations: str | Path
    after: str | Path | None = None
    base: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Describe only file selections; actual JSON null stays inside a file."""
        result: dict[str, object] = {
            "name": self.name,
            "before": _filename(self.before),
            "expectations": _filename(self.expectations),
        }
        if self.after is not None:
            result["after"] = _filename(self.after)
        if self.base is not None:
            result["base"] = self.base
        return result


@dataclass(frozen=True)
class ApplicationInput:
    """One named vocabulary scope and a self-contained application record file."""

    source: str
    application: str | Path
    lookup: tuple[str, ...] = ()
    graph_name: str | None = None
    union: bool = False

    def __post_init__(self) -> None:
        """Own the finite lookup inventory independently of caller lists."""
        object.__setattr__(self, "lookup", _capture(self.lookup, Budget()))

    def to_dict(self) -> dict[str, object]:
        """Reuse the ordinary vocabulary selection and application input schema."""
        result: dict[str, object] = {
            "source": self.source,
            "application": _filename(self.application),
            "lookup": list(self.lookup),
            "union": self.union,
        }
        if self.graph_name is not None:
            result["graph_name"] = self.graph_name
        return result


class ProjectBuilder:
    """Build version 2 JSON through ProjectConfig; never run a separate engine.

    Resource methods store filenames, not live RDF graphs or serializer objects.
    Job metadata is captured when added. Build performs the same configuration
    validation as loading the resulting JSON through the ordinary CLI.
    """

    def __init__(
        self,
        *,
        directory: Path,
        limits: Limits | None = None,
        strict: bool = False,
    ) -> None:
        self.directory = directory
        self.limits = limits if limits is not None else Limits()
        self.strict = strict
        self._budget = Budget(self.limits)
        self._rdf: dict[str, object] = {}
        self._contexts: dict[str, object] = {}
        self._checks: list[object] = []

    def _own(self, value: object) -> object:
        """Own JSON metadata and bound cumulative retained builder storage."""
        result = own_json(
            value, max_bytes=self.limits.max_bytes, max_depth=self.limits.max_depth
        )
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        bound_text(encoded, self._budget)
        return json.loads(encoded)

    def _add(self, job: dict[str, object], depends_on: Sequence[str]) -> Self:
        self._budget.consume("states")
        job["depends_on"] = list(_capture(depends_on, self._budget))
        self._checks.append(self._own(job))
        return self

    def _context_files(self, values: Mapping[str, str | Path]) -> dict[str, object]:
        """Bound the finite filename mapping before retaining its entries."""
        if not isinstance(values, Mapping):
            raise InputError("Corpus context files require a finite mapping")
        count = len(values)
        self._budget.consume("states", count)
        result: dict[str, object] = {}
        for index, (iri, path) in enumerate(values.items()):
            self._budget.consume("work")
            if index >= count:
                raise InputError("Corpus context files changed during capture")
            bound_text(iri, self._budget)
            key = str(iri)
            if key in result:
                raise InputError("Duplicate corpus context file")
            result[key] = _filename(path)
        if len(result) != count:
            raise InputError("Corpus context files changed during capture")
        return result

    def rdf(
        self,
        name: str,
        path: str | Path,
        *,
        format: str | None = None,
        iri: str | None = None,
        effective_url: str | None = None,
        graph_name: str | None = None,
        union: bool = False,
    ) -> Self:
        """Register one explicit local RDF descriptor, acquiring no content."""
        bound_text(name, self._budget)
        name = str(name)
        if name in self._rdf:
            raise InputError("Duplicate builder RDF resource name")
        descriptor: dict[str, object] = {"path": _filename(path), "union": union}
        descriptor.update(
            {
                key: value
                for key, value in {
                    "format": format,
                    "iri": iri,
                    "effective_url": effective_url,
                    "graph_name": graph_name,
                }.items()
                if value is not None
            }
        )
        self._budget.consume("resources")
        self._rdf[name] = self._own(descriptor)
        return self

    def context(self, iri: str, path: str | Path) -> Self:
        """Register an offline logical context without opening its file."""
        bound_text(iri, self._budget)
        iri = str(iri)
        if iri in self._contexts:
            raise InputError("Duplicate builder context resource")
        self._budget.consume("resources")
        self._contexts[iri] = self._own(_filename(path))
        return self

    def vocabulary(
        self,
        name: str,
        *,
        source: str,
        inventory: str | Path,
        depends_on: Sequence[str] = (),
    ) -> Self:
        """Select the existing vocabulary inventory checker."""
        return self._add(
            {
                "name": name,
                "kind": "vocabulary",
                "source": source,
                "inventory": _filename(inventory),
            },
            depends_on,
        )

    def models(
        self,
        name: str,
        *,
        source: str,
        declarations: str | Path,
        depends_on: Sequence[str] = (),
    ) -> Self:
        """Select the existing model declaration checker."""
        return self._add(
            {
                "name": name,
                "kind": "models",
                "source": source,
                "declarations": _filename(declarations),
            },
            depends_on,
        )

    def emission(
        self,
        name: str,
        *,
        source: str,
        declarations: str | Path,
        document: str | Path,
        usages: str | Path,
        base: str | None = None,
        allowed_drops: Sequence[str] = (),
        rdf_direction: str | None = None,
        depends_on: Sequence[str] = (),
    ) -> Self:
        """Select checks of actual serialized output and explicit model bindings."""
        job: dict[str, object] = {
            "name": name,
            "kind": "emission",
            "source": source,
            "declarations": _filename(declarations),
            "document": _filename(document),
            "usages": _filename(usages),
            "allowed_drops": list(_capture(allowed_drops, self._budget)),
        }
        if base is not None:
            job["base"] = base
        if rdf_direction is not None:
            job["rdf_direction"] = rdf_direction
        return self._add(job, depends_on)

    def context_corpus(
        self,
        name: str,
        *,
        documents: Sequence[CorpusDocumentInput],
        before_contexts: Mapping[str, str | Path],
        after_contexts: Mapping[str, str | Path],
        branches: Sequence[CorpusBranch] = (),
        rdf_direction: str | None = None,
        depends_on: Sequence[str] = (),
    ) -> Self:
        """Select existing corpus checks with distinct old/new offline resources."""
        captured = _capture(documents, self._budget)
        if any(not isinstance(item, CorpusDocumentInput) for item in captured):
            raise InputError("Expected typed corpus document file selections")
        coverage = []
        for branch in _capture(branches, self._budget):
            if not isinstance(branch, CorpusBranch):
                raise InputError("Expected typed corpus coverage branches")
            coverage.append(
                {
                    "name": branch.name,
                    "minimum": branch.minimum,
                    "witnesses": [
                        {
                            "document": item.document,
                            "path": list(_capture(item.path, self._budget)),
                        }
                        for item in _capture(branch.witnesses, self._budget)
                    ],
                }
            )
        job: dict[str, object] = {
            "name": name,
            "kind": "context-corpus",
            "documents": [item.to_dict() for item in captured],
            "before_contexts": self._context_files(before_contexts),
            "after_contexts": self._context_files(after_contexts),
            "branches": coverage,
        }
        if rdf_direction is not None:
            job["rdf_direction"] = rdf_direction
        return self._add(job, depends_on)

    def upgrade(
        self,
        name: str,
        *,
        before: ApplicationInput,
        after: ApplicationInput,
        scopes: Sequence[str] = ("local",),
        depends_on: Sequence[str] = (),
    ) -> Self:
        """Select explicit paired application releases without a serializer shim."""
        if not isinstance(before, ApplicationInput) or not isinstance(
            after, ApplicationInput
        ):
            raise InputError("Expected typed application input selections")
        return self._add(
            {
                "name": name,
                "kind": "upgrade",
                "before": before.to_dict(),
                "after": after.to_dict(),
                "scopes": list(_capture(scopes, self._budget)),
            },
            depends_on,
        )

    def build(self, *, source: str = "") -> ProjectConfig:
        """Use the same strict schema as load_project and installed CLI execution."""
        return ProjectConfig(
            {
                "version": 2,
                "rdf": self._rdf,
                "contexts": self._contexts,
                "checks": self._checks,
                "policy": {"strict": self.strict},
                "limits": {
                    name: getattr(self.limits, name)
                    for name in self.limits.__dataclass_fields__
                },
            },
            directory=self.directory,
            source=source,
        )
