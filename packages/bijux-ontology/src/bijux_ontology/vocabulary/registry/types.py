"""Owned namespace declarations and explicit identifier interpretation."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.diagnostics.reports import Issue
from bijux_ontology.rdf.identifiers import absolute_iri
from bijux_ontology.vocabulary.registry.prefixes import compact_iri, expand_curie


def _prefix(value: str) -> str:
    """Accept nonempty prefix labels without whitespace or a colon."""
    if (
        not isinstance(value, str)
        or not value
        or ":" in value
        or any(character.isspace() for character in value)
    ):
        raise InputError("Prefix labels must be nonempty without whitespace or a colon")
    return value


@dataclass(frozen=True)
class NamespaceContribution:
    """One owner's exact namespace, preferred spelling and permitted aliases."""

    namespace: str
    canonical_prefix: str
    owner: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        absolute_iri(self.namespace)
        _prefix(self.canonical_prefix)
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise InputError("A namespace contribution requires a nonempty owner")
        if isinstance(cast(object, self.aliases), (str, bytes)) or not isinstance(
            self.aliases, Sequence
        ):
            raise InputError("Namespace aliases require a sequence of prefix labels")
        aliases = tuple(sorted({_prefix(alias) for alias in self.aliases}))
        object.__setattr__(self, "aliases", aliases)

    @property
    def prefixes(self) -> tuple[str, ...]:
        """Return every permitted spelling, including the preferred prefix."""
        return tuple(sorted({self.canonical_prefix, *self.aliases}))


def _resolve(
    contributions: Sequence[NamespaceContribution],
    canonical_prefixes: Mapping[str, str] | None,
) -> tuple[
    tuple[NamespaceContribution, ...], tuple[tuple[str, str], ...], tuple[Issue, ...]
]:
    """Validate owned contributions and resolve preferences without overwriting."""
    if isinstance(cast(object, contributions), (str, bytes)) or not isinstance(
        contributions, Sequence
    ):
        raise InputError("A registry requires namespace contributions")
    captured = tuple(contributions)
    if any(not isinstance(item, NamespaceContribution) for item in captured):
        raise InputError("A registry requires typed namespace contributions")
    owned = tuple(
        sorted(
            set(captured),
            key=lambda item: (
                item.namespace,
                item.canonical_prefix,
                item.owner,
                item.aliases,
            ),
        )
    )
    if canonical_prefixes is not None and not isinstance(canonical_prefixes, Mapping):
        raise InputError("Canonical prefix choices require a mapping")
    choices = dict(canonical_prefixes) if canonical_prefixes is not None else {}
    for namespace, prefix in choices.items():
        absolute_iri(namespace)
        _prefix(prefix)
    namespaces: dict[str, list[NamespaceContribution]] = {}
    bindings: dict[str, dict[str, set[str]]] = {}
    for item in owned:
        namespaces.setdefault(item.namespace, []).append(item)
        for prefix in item.prefixes:
            bindings.setdefault(prefix, {}).setdefault(item.namespace, set()).add(
                item.owner
            )
    unknown = set(choices) - namespaces.keys()
    if unknown:
        raise InputError(
            "Canonical choices require a contributed namespace", location=min(unknown)
        )
    issues: list[Issue] = []
    for prefix, values in sorted(bindings.items()):
        if len(values) > 1:
            issues.append(
                Issue(
                    "vocabulary.registry_alias_conflict",
                    "A prefix is contributed for different namespaces",
                    prefix,
                    kind="input_error",
                    evidence_json=json.dumps(
                        {
                            "prefix": prefix,
                            "bindings": [
                                {"namespace": namespace, "owners": sorted(owners)}
                                for namespace, owners in sorted(values.items())
                            ],
                        }
                    ),
                )
            )
    selected: list[tuple[str, str]] = []
    for namespace, items in sorted(namespaces.items()):
        permitted = {prefix for item in items for prefix in item.prefixes}
        preferences = {item.canonical_prefix for item in items}
        if namespace in choices:
            prefix = choices[namespace]
            if prefix not in permitted:
                raise InputError(
                    "Canonical prefix must be a contributed permitted alias",
                    location=namespace,
                )
        elif len(preferences) == 1:
            prefix = next(iter(preferences))
        else:
            issues.append(
                Issue(
                    "vocabulary.registry_canonical_conflict",
                    "Different preferred prefixes require an explicit canonical choice",
                    namespace,
                    kind="input_error",
                    evidence_json=json.dumps(
                        {
                            "namespace": namespace,
                            "preferences": [
                                {"prefix": item.canonical_prefix, "owner": item.owner}
                                for item in items
                            ],
                        }
                    ),
                )
            )
            continue
        selected.append((namespace, prefix))
    return owned, tuple(selected), tuple(issues)


@dataclass(frozen=True, init=False)
class IdentifierRegistry:
    """Immutable, validated namespace policy with separate IRI and CURIE intent."""

    contributions: tuple[NamespaceContribution, ...]
    canonical_prefixes: tuple[tuple[str, str], ...]
    bindings: tuple[tuple[str, str], ...]

    def __init__(
        self,
        contributions: Sequence[NamespaceContribution],
        *,
        canonical_prefixes: Mapping[str, str] | None = None,
    ) -> None:
        owned, canonical, issues = _resolve(contributions, canonical_prefixes)
        if issues:
            raise InputError(issues[0].message, location=issues[0].location)
        bindings = tuple(
            sorted(
                {(prefix, item.namespace) for item in owned for prefix in item.prefixes}
            )
        )
        object.__setattr__(self, "contributions", owned)
        object.__setattr__(self, "canonical_prefixes", canonical)
        object.__setattr__(self, "bindings", bindings)

    def iri(self, value: str) -> str:
        """Validate an absolute IRI and preserve its exact spelling."""
        return absolute_iri(value)

    def expand_curie(self, value: str) -> str:
        """Interpret the supplied spelling only as a registered CURIE."""
        return expand_curie(value, dict(self.bindings))

    def compact_iri(self, value: str) -> str:
        """Use the longest matching namespace and its explicit canonical prefix."""
        return compact_iri(
            value, {prefix: namespace for namespace, prefix in self.canonical_prefixes}
        )
