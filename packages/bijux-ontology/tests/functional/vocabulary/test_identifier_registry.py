"""Canonical aliases preserve explicit intent, ownership and immutable policy."""

from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest

from bijux_ontology.errors import InputError
from bijux_ontology.vocabulary.registry import (
    IdentifierRegistry,
    NamespaceContribution,
    merge_identifier_registry,
)

EX = "https://example.org/"


def test_aliases_nested_namespaces_and_explicit_choices_ignore_insertion_order() -> (
    None
):
    declarations = (
        NamespaceContribution(EX, "ex", "local", ("example",)),
        NamespaceContribution(EX, "example", "consumer", ("e",)),
        NamespaceContribution(EX + "nested/", "nested", "local", ("n",)),
    )
    for order in (declarations, tuple(reversed(declarations))):
        merged = merge_identifier_registry(order, canonical_prefixes={EX: "e"})
        assert merged.report.verified and merged.registry is not None
        registry = merged.registry
        assert registry.compact_iri(EX + "A") == "e:A"
        assert registry.compact_iri(EX + "nested/A") == "nested:A"
        assert registry.expand_curie("example:A") == EX + "A"
        assert registry.expand_curie("n:A") == EX + "nested/A"
        assert registry.compact_iri("urn:unregistered") == "urn:unregistered"
        assert (
            registry.expand_curie(registry.compact_iri(EX + "nested/A"))
            == EX + "nested/A"
        )
    assert IdentifierRegistry(declarations, canonical_prefixes={EX: "e"}) == registry


def test_atomic_merge_retains_all_owners_and_never_resolves_alias_conflicts() -> None:
    declarations = (
        NamespaceContribution(EX, "ex", "first", ("shared",)),
        NamespaceContribution("https://other.example/", "other", "second", ("shared",)),
    )
    result = merge_identifier_registry(declarations)
    assert result.registry is None and result.report.outcome == "input_error"
    issue = result.report.issues[0]
    assert issue.code == "vocabulary.registry_alias_conflict"
    assert issue.evidence == {
        "prefix": "shared",
        "bindings": [
            {"namespace": EX, "owners": ["first"]},
            {"namespace": "https://other.example/", "owners": ["second"]},
        ],
    }
    assert (
        merge_identifier_registry(declarations, canonical_prefixes={EX: "ex"}).registry
        is None
    )
    with pytest.raises(InputError, match="different namespaces"):
        IdentifierRegistry(declarations)
    conflicting_preferences = (
        declarations[0],
        NamespaceContribution(EX, "example", "third"),
    )
    conflict = merge_identifier_registry(conflicting_preferences)
    assert conflict.registry is None
    assert conflict.report.issues[0].code == "vocabulary.registry_canonical_conflict"
    assert merge_identifier_registry(
        conflicting_preferences, canonical_prefixes={EX: "shared"}
    ).report.verified


def test_explicit_iri_and_curie_operations_preserve_lexical_identity() -> None:
    registry = IdentifierRegistry((NamespaceContribution(EX, "urn", "consumer"),))
    assert registry.iri("urn:example") == "urn:example"
    assert registry.expand_curie("urn:example") == EX + "example"
    assert registry.iri("HTTPS://Example.org/%61") == "HTTPS://Example.org/%61"
    with pytest.raises(InputError, match="Unknown prefix"):
        registry.expand_curie("https://example.org/A")


def test_registry_owns_inputs_and_rejects_invalid_or_ambiguous_construction() -> None:
    aliases = ["example"]
    contribution = NamespaceContribution(
        EX, "ex", "caller", cast(tuple[str, ...], aliases)
    )
    declarations = [contribution]
    choices = {EX: "example"}
    registry = IdentifierRegistry(declarations, canonical_prefixes=choices)
    aliases.clear()
    declarations.clear()
    choices[EX] = "wrong"
    assert registry.expand_curie("example:A") == EX + "A"
    assert registry.compact_iri(EX + "A") == "example:A"
    assert registry.contributions[0].owner == "caller"
    with pytest.raises(FrozenInstanceError):
        registry.bindings = ()  # type: ignore[misc]
    for prefix in ("", "bad prefix", "bad:prefix", "bad\n"):
        with pytest.raises(InputError, match="Prefix labels"):
            NamespaceContribution(EX, prefix, "caller")
    with pytest.raises(InputError, match="permitted alias"):
        IdentifierRegistry((contribution,), canonical_prefixes={EX: "unknown"})
    with pytest.raises(InputError, match="contributed namespace"):
        IdentifierRegistry(
            (contribution,), canonical_prefixes={"urn:absent": "example"}
        )
    empty = merge_identifier_registry(())
    assert empty.registry is not None and not empty.report.verified
    assert empty.report.checked == 0


def test_runtime_containers_cannot_guess_policy_from_keys_or_duplicate_pairs() -> None:
    invalid: list[Any] = [None, 1, "alias", {"alias": "ignored"}, iter(("alias",))]
    for value in invalid:
        with pytest.raises(InputError):
            NamespaceContribution(EX, "ex", "caller", value)
        with pytest.raises(InputError):
            IdentifierRegistry(value)
        with pytest.raises(InputError):
            merge_identifier_registry(value)
    contribution = NamespaceContribution(EX, "ex", "caller", ("alias",))
    invalid_choices: list[Any] = ["bad", 1, [(EX, "ex"), (EX, "alias")]]
    for choices in invalid_choices:
        with pytest.raises(InputError, match="mapping"):
            merge_identifier_registry((contribution,), canonical_prefixes=choices)
