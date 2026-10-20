"""Explicit namespace inventory ownership cannot be filled by external lookup."""

from dataclasses import FrozenInstanceError

import pytest
from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import OWL, RDF, DefinedNamespace

from bijux_ontology.declarations.types import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology_adapters.namespaces import namespace_inventory

EX = Namespace("https://example.org/owned/")


class Parent(DefinedNamespace):
    """An explicitly declared inherited term."""

    lowerCase: URIRef
    _NS = EX


class Terms(Parent):
    """Annotations and nonidentifier extras form a finite inventory."""

    UpperCase: URIRef
    _extras = ["hyphen-name"]
    _underscore_num = True


def test_names_exact_iris_inheritance_kinds_and_snapshot_are_preserved() -> None:
    registry = {"alias": TermDeclaration(str(EX.UpperCase), (str(OWL.Class),))}
    inventory = namespace_inventory(
        {"app.Terms": Terms},
        registries={"app.registry": registry},
        kinds={"app.Terms.lowerCase": (str(OWL.Class),)},
        exclude=[str(EX["hyphen-name"])],
    )
    registry.clear()
    assert [
        (term.location, term.iri, term.kinds) for term in inventory.declarations
    ] == [
        ("app.Terms.lowerCase", str(EX.lowerCase), (str(OWL.Class),)),
        ("app.Terms.UpperCase", str(EX.UpperCase), ()),
        ("app.Terms.hyphen-name", str(EX["hyphen-name"]), ()),
        ("app.registry.alias", str(EX.UpperCase), (str(OWL.Class),)),
    ]
    assert inventory.exclusions == (str(EX["hyphen-name"]),)
    with pytest.raises(FrozenInstanceError):
        inventory.exclusions = ()  # type: ignore[misc]
    graph = Graph().add((EX.lowerCase, RDF.type, OWL.Class))
    graph.add((EX.UpperCase, RDF.type, OWL.Class))
    assert inventory.check(graph).verified
    wrong = namespace_inventory(
        {"Terms": Terms}, kinds={"Terms.UpperCase": (str(OWL.ObjectProperty),)}
    ).check(graph)
    assert any(
        issue.code == "vocabulary.entity_kind_mismatch" for issue in wrong.issues
    )


def test_reverse_coverage_is_opt_in_and_lookup_cannot_hide_local_absence() -> None:
    local = Graph().add((EX.lowerCase, RDF.type, OWL.Class))
    local.add((EX.UpperCase, RDF.type, OWL.Class))
    inventory = namespace_inventory(
        {}, registries={"registry": {"lowerCase": str(EX.lowerCase)}}
    )
    assert inventory.check(local, owned_namespaces=(str(EX),)).verified
    reverse = inventory.check(local, owned_namespaces=(str(EX),), require_coverage=True)
    assert [(issue.code, issue.actual) for issue in reverse.issues] == [
        ("vocabulary.coverage_gap", str(EX.UpperCase))
    ]
    local.remove((EX.lowerCase, None, None))
    lookup = Graph().add((EX.lowerCase, RDF.type, OWL.Class))
    sources = load_vocabulary(local, lookup=[lookup])
    assert inventory.check(sources).verified
    owned = inventory.check(sources, owned_namespaces=(str(EX),))
    assert [(issue.code, issue.actual) for issue in owned.issues] == [
        ("vocabulary.term_missing", str(EX.lowerCase))
    ]
    missing = inventory.check(
        sources, coverage_terms=(str(EX.lowerCase),), require_coverage=True
    )
    assert any(
        issue.code == "vocabulary.coverage_source_missing" for issue in missing.issues
    )
    unselected = inventory.check(sources, require_coverage=True)
    assert unselected.exit_code() == 2
    assert unselected.issues[0].code == "input.coverage_scope_missing"


def test_conflicting_registry_entries_and_misspelled_contracts_are_input_errors() -> (
    None
):
    with pytest.raises(InputError, match="Conflicting namespace declaration"):
        namespace_inventory(
            {"Terms": Terms},
            registries={"Terms": {"lowerCase": str(EX.different)}},
        )
    with pytest.raises(InputError, match="Conflicting accepted kinds"):
        namespace_inventory(
            {},
            registries={
                "terms": {"value": TermDeclaration(str(EX.value), (str(OWL.Class),))}
            },
            kinds={"terms.value": (str(OWL.ObjectProperty),)},
        )
    with pytest.raises(InputError, match="unknown declarations"):
        namespace_inventory({"Terms": Terms}, kinds={"Terms.absent": (str(OWL.Class),)})
    with pytest.raises(InputError, match="DefinedNamespace"):
        namespace_inventory({"Terms": object})  # type: ignore[dict-item]
