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
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.reports import AcceptancePolicy, IssueSelection, Severity


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
        if type(owned.get("version")) is not int or owned["version"] != 1:
            raise InputError("Project version must be 1", location=source + "/version")
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
                entry, {"path", "format", "iri", "effective_url"}, location=where
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
            )
        self.rdf: Mapping[str, RdfSource] = MappingProxyType(rdf)
        contexts = object_value(
            owned.get("contexts", {}), location=source + "/contexts"
        )
        self.contexts: Mapping[str, Path] = MappingProxyType(
            {
                absolute_iri(iri): self.path(path, location=source + "/contexts/" + iri)
                for iri, path in contexts.items()
            }
        )
        checks = owned.get("checks")
        if not isinstance(checks, list) or not checks:
            raise InputError(
                "A project requires a nonempty checks array",
                location=source + "/checks",
            )
        names = set()
        for index, raw in enumerate(checks):
            where = f"{source}/checks/{index}"
            check = object_value(raw, location=where)
            name = text_value(check.get("name"), location=where + "/name")
            text_value(check.get("kind"), location=where + "/kind")
            if name in names:
                raise InputError("Check names must be unique", location=where + "/name")
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

    def explain(self) -> dict[str, object]:
        """Describe only selected resources, paths, limits and acceptance policy."""
        checks = list(self.checks())
        for check in checks:
            members = check.get("documents", [])
            if not isinstance(members, list):
                raise InputError("Context batch documents must be an array")
            jobs = [
                check,
                *(object_value(member, location="documents") for member in members),
            ]
            for job in jobs:
                for key in ("inventory", "declarations", "document", "expectations"):
                    if (
                        check["kind"] in ("vocabulary", "models", "context")
                        and key in job
                    ):
                        job[key] = str(self.path(job[key]))
        return {
            "source": self.source,
            "directory": str(self.directory),
            "rdf": {
                name: {
                    "path": str(item.content),
                    "format": item.format,
                    "iri": item.document_iri,
                    "effective_url": item.effective_url,
                }
                for name, item in self.rdf.items()
            },
            "contexts": {iri: str(path) for iri, path in self.contexts.items()},
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
