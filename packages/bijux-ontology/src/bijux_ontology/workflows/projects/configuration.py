"""Owned, strict project configuration with paths relative to its source file."""

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType
from typing import cast

from bijux_ontology._json import (
    known_keys,
    object_value,
    own_json,
    read_json,
    text_value,
)
from bijux_ontology.errors import InputError
from bijux_ontology.jsonld.resources import DocumentLocation
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import AcceptancePolicy, IssueSelection, Severity
from bijux_ontology.workflows.projects.jobs import (
    APPLICATION_KINDS,
    check_kind,
    dependencies,
)
from bijux_ontology.workflows.projects.jobs import (
    input_paths as job_input_paths,
)


class ProjectConfig:
    """An owned project specification; inspecting it performs no source IO.

    RDF resources are named explicitly. Context resources use logical URLs.
    Check input filenames resolve against ``directory``, independently of cwd.
    Unknown check-specific options are validated by the selected checker runner.
    """

    def __init__(self, data: object, *, directory: Path, source: str = "") -> None:
        owned = object_value(own_json(data, source=source), location=source)
        known_keys(
            owned,
            {"version", "rdf", "contexts", "checks", "policy", "limits"},
            location=source,
        )
        if type(owned.get("version")) is not int or owned["version"] not in (1, 2):
            raise InputError(
                "Project version must be 1 or 2", location=source + "/version"
            )
        self.version = owned["version"]
        self.directory = directory.resolve()
        self.source = source
        self._data = owned
        raw_limits = object_value(owned.get("limits", {}), location=source + "/limits")
        known_keys(
            raw_limits, set(Limits.__dataclass_fields__), location=source + "/limits"
        )
        self.limits = Limits(**cast(dict[str, int], raw_limits))
        raw_policy = object_value(owned.get("policy", {}), location=source + "/policy")
        known_keys(
            raw_policy,
            {"strict", "minimum_severity", "exceptions"},
            location=source + "/policy",
        )
        exceptions = raw_policy.get("exceptions", [])
        if not isinstance(exceptions, list):
            raise InputError(
                "exceptions must be an array", location=source + "/policy/exceptions"
            )
        selections = []
        for index, item in enumerate(exceptions):
            where = f"{source}/policy/exceptions/{index}"
            entry = object_value(item, location=where)
            known_keys(entry, {"code", "location"}, location=where)
            selections.append(
                IssueSelection(
                    text_value(entry.get("code"), location=where),
                    text_value(entry.get("location"), location=where),
                )
            )
        self.policy = AcceptancePolicy(
            minimum_severity=cast(
                Severity, raw_policy.get("minimum_severity", "error")
            ),
            strict=cast(bool, raw_policy.get("strict", False)),
            exceptions=tuple(selections),
        )
        resources = object_value(owned.get("rdf", {}), location=source + "/rdf")
        rdf = {}
        logical_iris: set[str] = set()
        for name, item in resources.items():
            where = f"{source}/rdf/{name}"
            text_value(name, location=where)
            entry = object_value(item, location=where)
            known_keys(
                entry,
                {"path", "format", "iri", "effective_url", "graph_name", "union"},
                location=where,
            )
            for key in ("iri", "effective_url"):
                if entry.get(key) is not None:
                    absolute_iri(text_value(entry[key], location=where + "/" + key))
            syntax = entry.get("format")
            if syntax is not None:
                syntax = text_value(syntax, location=where + "/format")
            logical_iri = entry.get("iri")
            if isinstance(logical_iri, str):
                if logical_iri in logical_iris:
                    raise InputError(
                        "RDF resource logical IRIs must be unique",
                        location=where + "/iri",
                    )
                logical_iris.add(logical_iri)
            rdf[name] = RdfSource(
                self.path(entry.get("path"), location=where + "/path"),
                format=syntax,
                document_iri=cast(str | None, entry.get("iri")),
                effective_url=cast(str | None, entry.get("effective_url")),
                graph_name=cast(str | None, entry.get("graph_name")),
                union=cast(bool, entry.get("union", False)),
            )
        self.rdf: Mapping[str, RdfSource] = MappingProxyType(rdf)
        contexts = object_value(
            owned.get("contexts", {}), location=source + "/contexts"
        )
        context_paths = {}
        context_metadata = {}
        for iri, value in contexts.items():
            where = source + "/contexts/" + iri
            absolute_iri(iri)
            metadata = DocumentLocation()
            if isinstance(value, dict):
                entry = object_value(value, location=where)
                known_keys(
                    entry, {"path", "effective_url", "context_url"}, location=where
                )
                fields = {}
                for key in ("effective_url", "context_url"):
                    field = entry.get(key)
                    if field is not None:
                        field = absolute_iri(
                            text_value(field, location=where + "/" + key)
                        )
                    fields[key] = field
                metadata = DocumentLocation(**fields)
                value = entry.get("path")
            context_paths[iri] = self.path(value, location=where)
            context_metadata[iri] = metadata
        self.contexts: Mapping[str, Path] = MappingProxyType(context_paths)
        self.context_metadata: Mapping[str, DocumentLocation] = MappingProxyType(
            context_metadata
        )
        checks = owned.get("checks")
        if not isinstance(checks, list) or not checks:
            raise InputError(
                "A project requires a nonempty checks array",
                location=source + "/checks",
            )
        names: set[str] = set()
        for index, raw in enumerate(checks):
            where = f"{source}/checks/{index}"
            check = object_value(raw, location=where)
            name = text_value(check.get("name"), location=where + "/name")
            kind = text_value(check.get("kind"), location=where + "/kind")
            if name in names:
                raise InputError("Check names must be unique", location=where + "/name")
            if self.version == 1 and (
                kind in APPLICATION_KINDS or "depends_on" in check
            ):
                raise InputError(
                    "Application jobs and dependencies require project version 2",
                    location=where,
                )
            if self.version == 2:
                check_kind(check)
                if set(dependencies(check)) - names:
                    raise InputError(
                        "Job dependencies must name earlier declared checks",
                        location=where,
                    )
            names.add(name)

    def path(self, value: object, *, location: str = "") -> Path:
        """Resolve one declared filename against the containing configuration."""
        return (self.directory / text_value(value, location=location)).resolve()

    def to_dict(self) -> dict[str, object]:
        """Return a copy; callers cannot alter the retained project policy."""
        return deepcopy(self._data)

    def checks(self) -> tuple[dict[str, object], ...]:
        """Return independently owned selected jobs in their declared order."""
        return tuple(deepcopy(cast(list[dict[str, object]], self._data["checks"])))

    def input_paths(self) -> tuple[Path, ...]:
        """Return directly declared filenames without acquiring their contents."""
        paths = [cast(Path, item.content) for item in self.rdf.values()]
        paths.extend(self.contexts.values())
        for check in self.checks():
            paths.extend(self.path(value) for _, value in job_input_paths(check))
        return tuple(dict.fromkeys(paths))

    def _resolved_check(self, check: dict[str, object]) -> dict[str, object]:
        """Resolve only known file slots in this independently owned job copy."""
        for path, value in job_input_paths(check):
            current: object = check
            for part in path[:-1]:
                if isinstance(current, dict) and isinstance(part, str):
                    current = current.get(part)
                elif isinstance(current, list) and isinstance(part, int):
                    current = current[part]
                else:
                    raise InputError("Invalid configured input path")
            key = path[-1]
            if not isinstance(current, dict) or not isinstance(key, str):
                raise InputError("Invalid configured input filename")
            current[key] = str(self.path(value))
        return check

    def explain(self) -> dict[str, object]:
        """Describe only selected resources, paths, limits and acceptance policy."""
        checks = [self._resolved_check(check) for check in self.checks()]
        return {
            "source": self.source,
            "directory": str(self.directory),
            "rdf": {
                name: {
                    "path": str(item.content),
                    "format": item.format,
                    "iri": item.document_iri,
                    "effective_url": item.effective_url,
                    "graph_name": item.graph_name,
                    "union": item.union,
                }
                for name, item in self.rdf.items()
            },
            "contexts": {
                iri: str(path)
                if self.context_metadata[iri] == DocumentLocation()
                else {
                    "path": str(path),
                    "effective_url": self.context_metadata[iri].effective_url,
                    "context_url": self.context_metadata[iri].context_url,
                }
                for iri, path in self.contexts.items()
            },
            "checks": checks,
            "policy": {
                "strict": self.policy.strict,
                "minimum_severity": self.policy.minimum_severity,
                "exceptions": [
                    {"code": item.code, "location": item.location}
                    for item in self.policy.exceptions
                ],
            },
            "limits": {
                name: getattr(self.limits, name)
                for name in self.limits.__dataclass_fields__
            },
        }


def load_project(path: Path) -> ProjectConfig:
    """Read strict JSON project configuration relative to its own file."""
    selected = path.resolve()
    return ProjectConfig(
        read_json(selected), directory=selected.parent, source=str(selected)
    )
