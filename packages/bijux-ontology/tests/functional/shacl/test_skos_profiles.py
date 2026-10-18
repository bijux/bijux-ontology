"""Portable explicit SKOS label policies agree with native selected checks."""

from dataclasses import replace

import pytest
from rdflib import Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SH, SKOS, XSD

from bijux_ontology.documents.export import export_rdf
from bijux_ontology.errors import InputError
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.components import check_components
from bijux_ontology.shacl.coverage import ShaclCoverageContract
from bijux_ontology.shacl.skos_profiles import SkosProfile, compile_skos_profile
from bijux_ontology.skos.checking import check_skos
from bijux_ontology.skos.xl import XL

EX = Namespace("https://example.org/skos-profiles/")


def profile() -> SkosProfile:
    """Select actual named targets without an inferred targetClass boundary."""
    return SkosProfile(EX.Shape, (EX.C,), ("en",))


def native(graph: Graph, selected: SkosProfile) -> bool:
    """Use the original ordinary checker with exactly the exported policy inputs."""
    return check_skos(
        graph,
        concepts=tuple(map(str, selected.concepts)),
        rules=selected.rules,
        languages=selected.languages,
        language_fallback=selected.language_fallback,
    ).verified


def test_exact_language_and_integrity_native_backend_equivalence() -> None:
    pytest.importorskip("pyshacl")
    selected = profile()
    compiled = compile_skos_profile(selected, trusted_sparql=True)
    assert compiled.report.verified and compiled.components is not None
    assert set(compiled.translated) == set(selected.rules)
    cases = [
        (Graph().add((EX.C, SKOS.prefLabel, Literal("name", lang="en"))), True),
        (Graph().add((EX.C, SKOS.prefLabel, Literal("name", lang="EN"))), True),
        (Graph().add((EX.C, SKOS.prefLabel, Literal("name", lang="en-GB"))), False),
        (Graph().add((EX.C, SKOS.prefLabel, Literal("nom", lang="fr"))), False),
        (Graph(), False),
    ]
    base = cases[0][0]
    cases.extend(
        (
            (
                base + Graph().add((EX.C, SKOS.prefLabel, Literal("nom", lang="fr"))),
                True,
            ),
            (
                base + Graph().add((EX.C, SKOS.prefLabel, Literal("other", lang="EN"))),
                False,
            ),
            (
                base + Graph().add((EX.C, SKOS.altLabel, Literal("name", lang="en"))),
                False,
            ),
            (
                base
                + Graph().add((EX.C, SKOS.hiddenLabel, Literal("name", lang="EN"))),
                False,
            ),
            (
                base
                + Graph()
                .add((EX.C, SKOS.prefLabel, Literal("a")))
                .add((EX.C, SKOS.prefLabel, Literal("b"))),
                False,
            ),
            (
                base
                + Graph()
                .add((EX.C, SKOS.prefLabel, Literal("a")))
                .add((EX.C, SKOS.altLabel, Literal("a", datatype=XSD.string))),
                False,
            ),
            (
                base
                + Graph()
                .add((EX.C, SKOS.altLabel, Literal("a", lang="fr")))
                .add((EX.C, SKOS.hiddenLabel, Literal("a", lang="FR"))),
                False,
            ),
        )
    )
    for graph, expected in cases:
        before = frozenset(graph)
        assert native(graph, selected) is expected
        actual = check_components(
            prepare_rdf(graph), compiled.components, trusted_sparql=True
        )
        assert actual.result is not None and actual.result.conforms is expected
        assert actual.report.verified is expected
        assert frozenset(graph) == before

    xl_policy = replace(selected, rules=(*selected.rules, "xl"))
    xl_graph = Graph().add((EX.C, XL.prefLabel, EX.Label))
    xl_graph.add((EX.Label, XL.literalForm, Literal("name", lang="en")))
    assert native(xl_graph, xl_policy)
    xl_shapes = compile_skos_profile(xl_policy, trusted_sparql=True)
    assert not xl_shapes.report.verified and not xl_shapes.translated
    assert set(xl_shapes.untranslated) == set(xl_policy.rules)
    assert xl_shapes.source is not None and xl_shapes.components is None
    raw = validate_shapes(prepare_rdf(xl_graph), xl_shapes.source, preflight=True)
    assert raw.conforms


def test_fallback_languages_are_alternatives_without_restricting_other_labels() -> None:
    pytest.importorskip("pyshacl")
    selected = replace(profile(), languages=("en", "de"), language_fallback=("fr",))
    compiled = compile_skos_profile(selected, trusted_sparql=True)
    assert compiled.report.verified and compiled.components is not None
    assert len(compiled.components.roots) == 2
    for graph, expected in (
        (Graph().add((EX.C, SKOS.prefLabel, Literal("nom", lang="FR"))), True),
        (Graph().add((EX.C, SKOS.prefLabel, Literal("nom", lang="fr-CA"))), False),
        (Graph().add((EX.C, SKOS.prefLabel, Literal("name", lang="en"))), False),
        (
            Graph()
            .add((EX.C, SKOS.prefLabel, Literal("name", lang="en")))
            .add((EX.C, SKOS.prefLabel, Literal("Name", lang="de")))
            .add((EX.C, SKOS.prefLabel, Literal("other", lang="ja"))),
            True,
        ),
    ):
        assert native(graph, selected) is expected
        checked = check_components(
            prepare_rdf(graph), compiled.components, trusted_sparql=True
        )
        assert checked.result is not None and checked.result.conforms is expected
        assert checked.report.verified is expected


def test_portable_export_explicit_targets_and_physical_graph_selection() -> None:
    pytest.importorskip("pyshacl")
    selected = replace(profile(), shape=URIRef("https://example.org/#label-profile"))
    compiled = compile_skos_profile(selected, trusted_sparql=True)
    assert compiled.source is not None and compiled.components is not None
    graph = compiled.source.graph()
    assert not tuple(graph.triples((None, SH.targetClass, None)))
    assert set(graph.objects(selected.shape, SH.targetNode)) == {EX.C}
    exported = export_rdf(compiled.source, format="turtle")
    assert exported.report.verified and exported.content is not None
    reloaded = prepare_rdf(exported.content, format="turtle")
    dataset = Dataset()
    dataset.graph(EX.Selected).add((EX.C, SKOS.prefLabel, Literal("name", lang="en")))
    dataset.graph(EX.Selected).add((EX.Other, RDF.type, EX.Child))
    dataset.graph(EX.Selected).add((EX.Child, RDFS.subClassOf, SKOS.Concept))
    dataset.graph(EX.Hidden).add((EX.C, SKOS.prefLabel, Literal("conflict", lang="en")))
    source = prepare_rdf(RdfSource(dataset, graph_name=str(EX.Selected)))
    assert native(source.graph(), selected)
    actual = validate_shapes(
        source,
        reloaded,
        trusted_sparql=True,
        preflight=True,
        use_shapes=compiled.components.roots,
        required_constraints=compiled.components.requirements,
        coverage=ShaclCoverageContract(),
    )
    assert actual.conforms and actual.report.verified
    source.graph().remove((None, None, None))
    assert source.graph()
    untrusted = validate_shapes(source, reloaded, preflight=True)
    assert not untrusted.report.verified


def test_untranslated_rules_and_representation_assumptions_remain_required() -> None:
    selected = profile()
    untrusted = compile_skos_profile(selected)
    assert untrusted.source is not None and not untrusted.report.verified
    assert set(untrusted.untranslated) == {"label-integrity", "language-coverage"}
    partial = compile_skos_profile(
        replace(selected, rules=("label-integrity",)), trusted_sparql=True
    )
    assert not partial.report.verified and "label-integrity" in partial.untranslated
    assert "lexical-labels" not in partial.translated
    unsupported = compile_skos_profile(
        replace(selected, rules=(*selected.rules, "xl", "acyclic")), trusted_sparql=True
    )
    assert not unsupported.report.verified
    assert set(unsupported.untranslated) == {*selected.rules, "xl", "acyclic"}
    assert unsupported.source is not None
    for candidate in (replace(selected, concepts=()), replace(selected, languages=())):
        assert not compile_skos_profile(candidate, trusted_sparql=True).report.verified
    with pytest.raises(InputError):
        replace(selected, concepts=(URIRef("relative"),))
    with pytest.raises(InputError):
        replace(selected, concepts=(EX.C, EX.C))


def test_shared_budget_metadata_and_paid_untranslated_prefix() -> None:
    selected = replace(profile(), rules=(*profile().rules, "acyclic"))
    measured = Budget()
    compiled = compile_skos_profile(selected, trusted_sparql=True, budget=measured)
    assert not compiled.report.verified and "acyclic" in compiled.untranslated
    limit = max(1, measured._counts["work"] - 1)
    bounded = compile_skos_profile(
        selected, trusted_sparql=True, limits=Limits(max_work=limit)
    )
    assert bounded.report.outcome == "exhausted"
    assert any(issue.location == "acyclic" for issue in bounded.report.issues)
    assert bounded.source is None
    oversized = replace(profile(), shape=URIRef("urn:" + "x" * 10000))
    tiny = compile_skos_profile(
        oversized, trusted_sparql=True, limits=Limits(max_bytes=1000)
    )
    assert tiny.report.outcome == "exhausted" and tiny.source is None
    assert tiny.profile is None and not tiny.assumptions
    shared = Budget(Limits(max_work=measured._counts["work"] + 1))
    first = compile_skos_profile(selected, trusted_sparql=True, budget=shared)
    second = compile_skos_profile(selected, trusted_sparql=True, budget=shared)
    assert first.report.outcome != "exhausted" and second.report.outcome == "exhausted"
    assert (
        compile_skos_profile(selected, trusted_sparql=True).report.outcome
        != "exhausted"
    )
