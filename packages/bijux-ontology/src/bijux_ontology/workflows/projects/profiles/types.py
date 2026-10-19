"""Immutable profile metadata and reviewable selected-layer composition."""

from dataclasses import dataclass, field
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

from bijux_ontology.diagnostics.reports import Report
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.workflows.projects.bundles import PreparedBundle


@dataclass(frozen=True)
class ProfileParent:
    """A direct declared parent; optionality never removes another required edge."""

    iri: str
    required: bool = True


@dataclass(frozen=True)
class ProfileShapes:
    """Explicit named roots for scoped definition-conflict review, not new jobs."""

    source: str
    roots: tuple[str, ...]


@dataclass(frozen=True)
class ProfileArtifact:
    """A descriptive portable artifact and caller-declared PROF resource roles."""

    iri: str
    path: str
    roles: tuple[str, ...]
    format: str | None = None


@dataclass(frozen=True)
class ValidationProfile:
    """Owned manifest metadata; ordinary bundle acquisition happens parent-first."""

    iri: str
    parents: tuple[ProfileParent, ...]
    shapes: tuple[ProfileShapes, ...]
    artifacts: tuple[ProfileArtifact, ...]
    same_shape: Literal["reject", "separate"]
    severity_changes: Literal["reject", "preserve_each"]
    source: Path | Traversable
    manifest: bytes


@dataclass(frozen=True)
class ProfileLayer:
    """An included or explicitly excluded layer with its own immutable bundle."""

    iri: str
    profile: ValidationProfile | None
    bundle: PreparedBundle | None
    required: bool
    selected: bool
    jobs: tuple[str, ...] = ()
    execution_digest: str | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class ProfileDecision:
    """An explicit separate-definition decision retaining both original checks."""

    shape: str
    before: str
    after: str
    action: Literal["separate"] = "separate"


@dataclass(frozen=True)
class DescribedArtifact:
    """A PROF artifact IRI and roles, with no inferred local file binding."""

    iri: str
    roles: tuple[str, ...]
    format: str | None = None


@dataclass(frozen=True)
class ProfileDescription:
    """Descriptive PROF interchange; no executable checks or resource acquisition."""

    iri: str
    parents: tuple[str, ...]
    artifacts: tuple[DescribedArtifact, ...]
    source: PreparedSource | None
    report: Report
