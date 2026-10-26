"""Consumer naming keys never change SKOS integrity or original RDF terms."""

from collections.abc import Iterator, Sequence
from typing import cast

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, SKOS, XSD

from bijux_ontology.diagnostics.errors import InputError
from bijux_ontology.execution.limits import Limits
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.labels import Comparison, LabelHomonym, check_label_collisions
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://example.org/")


def _graph(*labels: Literal) -> Graph:
    """Supply independently identified concepts in one explicit scheme."""
    graph = Graph()
    for index, label in enumerate(labels):
        owner = EX[f"concept{index}"]
        graph.add((owner, RDF.type, SKOS.Concept))
        graph.add((owner, SKOS.inScheme, EX.scheme))
        graph.add((owner, SKOS.prefLabel, label))
    return graph


def test_homonyms_are_legal_skos_but_selected_naming_collisions() -> None:
    graph = _graph(Literal("shared", lang="en"), Literal("shared", lang="en"))
    assert check_skos(graph).verified
    report = check_label_collisions(graph, schemes=(EX.scheme,))
    assert report.outcome == "disagreement"
    issue = report.issues[0]
    assert issue.code == "skos.policy.label_collision"
    assert issue.evidence is not None
    assertions = cast(list[dict[str, str]], issue.evidence["assertions"])
    assert {entry["label"] for entry in assertions} == {'"shared"@en'}
    assert {entry["concept"] for entry in assertions} == {
        EX.concept0.n3(),
        EX.concept1.n3(),
    }
    assert check_label_collisions(
        graph, schemes=(EX.scheme,), concepts=(EX.concept0,)
    ).verified


@pytest.mark.parametrize(
    ("left", "right", "mode", "collides"),
    [
        ("Straße", "STRASSE", "exact", False),
        ("Straße", "STRASSE", "casefold", True),
        ("é", "e\u0301", "exact", False),
        ("é", "e\u0301", "nfc", True),
        ("É", "e\u0301", "nfc-casefold", True),
        (" label", "label", "nfc-casefold", False),
    ],
)
def test_named_comparison_modes_change_only_keys(
    left: str, right: str, mode: str, collides: bool
) -> None:
    graph = _graph(Literal(left), Literal(right, datatype=XSD.string))
    before = set(graph)
    report = check_label_collisions(
        graph, schemes=(EX.scheme,), comparison=cast(Comparison, mode)
    )
    assert (report.outcome == "disagreement") is collides
    assert report.verified is not collides
    assert set(graph) == before


def test_schemes_languages_and_selected_roles_are_explicit_namespaces() -> None:
    graph = _graph(Literal("same", lang="en"), Literal("same", lang="fr"))
    graph.remove((EX.concept1, SKOS.inScheme, EX.scheme))
    graph.add((EX.other, SKOS.hasTopConcept, EX.concept1))
    assert check_label_collisions(graph, schemes=(EX.scheme, EX.other)).verified
    graph.add((EX.concept1, SKOS.topConceptOf, EX.scheme))
    assert check_label_collisions(
        graph, schemes=(EX.scheme,), languages=("en",)
    ).verified
    graph.add((EX.concept1, SKOS.altLabel, Literal("same", lang="en")))
    assert check_label_collisions(graph, schemes=(EX.scheme,)).verified
    report = check_label_collisions(
        graph,
        schemes=(EX.scheme,),
        languages=("EN",),
        roles=(SKOS.prefLabel, SKOS.altLabel),
    )
    assert report.outcome == "disagreement"
    assert check_label_collisions(
        graph, schemes=(EX.scheme,), languages=(None,)
    ).verified


def test_homonym_allowances_require_one_explicit_owner_set() -> None:
    label = Literal("shared", lang="en")
    graph = _graph(label, label)
    allowed = LabelHomonym(EX.scheme, label, (EX.concept0, EX.concept1))
    report = check_label_collisions(
        graph, schemes=(EX.scheme,), allowed_homonyms=(allowed,)
    )
    assert report.verified and len(report.exclusions) == 1
    graph.add((EX.concept2, RDF.type, SKOS.Concept))
    graph.add((EX.concept2, SKOS.inScheme, EX.scheme))
    graph.add((EX.concept2, SKOS.prefLabel, label))
    overlapping = LabelHomonym(EX.scheme, label, (EX.concept1, EX.concept2))
    assert (
        check_label_collisions(
            graph,
            schemes=(EX.scheme,),
            allowed_homonyms=(allowed, overlapping),
        ).outcome
        == "disagreement"
    )
    wrong_language = LabelHomonym(
        EX.scheme, Literal("shared", lang="fr"), allowed.concepts
    )
    assert (
        check_label_collisions(
            _graph(label, label),
            schemes=(EX.scheme,),
            allowed_homonyms=(wrong_language,),
        ).outcome
        == "disagreement"
    )


def test_owned_view_and_unsupported_or_missing_scope_are_truthful() -> None:
    graph = _graph(Literal("one"), Literal("two"))
    view = inspect_vocabulary(graph)
    graph.set((EX.concept1, SKOS.prefLabel, Literal("one")))
    assert check_label_collisions(view, schemes=(EX.scheme,)).verified
    assert check_label_collisions(graph, schemes=(EX.scheme,)).outcome == "disagreement"
    assert check_label_collisions(view, schemes=(EX.absent,)).outcome == "unverified"
    assert (
        check_label_collisions(
            view, schemes=(EX.scheme,), concepts=(EX.absent,)
        ).outcome
        == "unverified"
    )
    malformed = _graph(Literal("1", datatype=XSD.integer))
    assert (
        check_label_collisions(malformed, schemes=(EX.scheme,)).outcome == "unverified"
    )
    # A known untagged integer is outside an explicitly English label selection.
    assert check_label_collisions(
        malformed, schemes=(EX.scheme,), languages=("en",)
    ).verified


def test_indexed_selections_and_exhaustion_retain_known_collisions() -> None:
    class Indexed(Sequence[str]):
        """A finite selection whose iterator must never be trusted."""

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int | slice) -> str:
            assert index == 0
            return str(EX.scheme)

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("Do not iterate caller selections")

    graph = _graph(Literal("same"), Literal("same"))
    for index in range(2, 20):
        graph.add((EX[f"later{index:02}"], RDF.type, SKOS.Concept))
        graph.add((EX[f"later{index:02}"], SKOS.inScheme, EX.scheme))
        graph.add(
            (
                EX[f"later{index:02}"],
                SKOS.prefLabel,
                Literal("later collision" if index in (2, 3) else str(index)),
            )
        )
    view = inspect_vocabulary(graph)
    assert check_label_collisions(view, schemes=Indexed()).outcome == "disagreement"
    partial = check_label_collisions(
        view, schemes=Indexed(), limits=Limits(max_issues=1)
    )
    assert not partial.complete
    assert any(issue.code == "skos.policy.label_collision" for issue in partial.issues)
    assert (
        check_label_collisions(
            view, schemes=(EX.scheme,), limits=Limits(max_states=1)
        ).outcome
        == "exhausted"
    )


@pytest.mark.parametrize(
    "options",
    [
        {"schemes": ()},
        {"schemes": (EX.scheme,), "roles": (EX.custom,)},
        {"schemes": (EX.scheme,), "comparison": "accent-insensitive"},
        {
            "schemes": (EX.scheme,),
            "allowed_homonyms": (
                LabelHomonym(EX.scheme, Literal("x"), (EX.concept0,)),
            ),
        },
    ],
)
def test_invalid_policy_inputs_do_not_create_qualified_reports(
    options: dict[str, object],
) -> None:
    with pytest.raises(InputError):
        check_label_collisions(_graph(Literal("x")), **options)  # type: ignore[arg-type]


def test_import_scopes_are_owned_and_original_assertion_sources_retained() -> None:
    local = _graph(Literal("shared"))
    local.add((EX.vocabulary, OWL.imports, EX.imported))
    imported = Graph()
    imported.add((EX.concept1, RDF.type, SKOS.Concept))
    imported.add((EX.concept1, SKOS.inScheme, EX.scheme))
    imported.add((EX.concept1, SKOS.prefLabel, Literal("shared")))
    vocabulary = load_vocabulary(local, imports={str(EX.imported): imported})
    assert check_label_collisions(
        vocabulary, schemes=(EX.scheme,), scopes=("local",)
    ).verified
    report = check_label_collisions(vocabulary, schemes=(EX.scheme,))
    assert report.outcome == "disagreement"
    evidence = report.issues[0].evidence
    assert evidence is not None
    assertions = cast(list[dict[str, str]], evidence["assertions"])
    assert {item["scope"] for item in assertions} == {"local", "import[0]"}


def test_label_resources_and_untyped_members_are_not_invented_concepts() -> None:
    graph = _graph(Literal("same", lang="en"))
    xl = Namespace("http://www.w3.org/2008/05/skos-xl#")
    graph.add((EX.label, RDF.type, xl.Label))
    for owner in (EX.label, EX.untyped):
        graph.add((owner, SKOS.inScheme, EX.scheme))
        graph.add((owner, SKOS.prefLabel, Literal("same", lang="en")))
    assert check_skos(graph).verified
    assert check_label_collisions(graph, schemes=(EX.scheme,)).verified
    assert (
        check_label_collisions(
            graph, schemes=(EX.scheme,), concepts=(EX.untyped,)
        ).outcome
        == "unverified"
    )
    # The established semantic-relation domain supplies the finite Concept role.
    graph.add((EX.untyped, SKOS.related, EX.concept0))
    report = check_label_collisions(graph, schemes=(EX.scheme,))
    assert report.outcome == "disagreement"
    evidence = report.issues[0].evidence
    assert evidence is not None
    assertions = cast(list[dict[str, str]], evidence["assertions"])
    assert {item["concept"] for item in assertions} == {
        EX.concept0.n3(),
        EX.untyped.n3(),
    }


@pytest.mark.parametrize("warm", [False, True])
def test_collision_preparation_respects_shared_source_admission(warm: bool) -> None:
    view = inspect_vocabulary(_graph(Literal("distinct")))
    if warm:
        assert check_label_collisions(view, schemes=(EX.scheme,)).verified
    result = check_label_collisions(
        view, schemes=(EX.scheme,), limits=Limits(max_triples=1)
    )
    assert result.outcome == "exhausted" and not result.complete
    assert not result.verified
