"""Scoped vocabulary loading preserves ownership, imports and source boundaries."""

from io import BytesIO
from pathlib import Path

import pytest
from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, XSD

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

EX = Namespace("https://example.org/")
TURTLE = "@prefix ex:<https://example.org/>. ex:Image a <http://www.w3.org/2002/07/owl#Class>."


def test_direct_constructor_and_every_graph_export_are_owned() -> None:
    local = Graph().add((EX.Image, RDF.type, OWL.Class))
    lookup = Graph().add((EX.Other, RDF.type, OWL.Class))
    vocabulary = Vocabulary(local, lookup=[lookup])
    local.remove((EX.Image, RDF.type, OWL.Class))
    lookup.remove((EX.Other, RDF.type, OWL.Class))
    vocabulary.local.remove((EX.Image, RDF.type, OWL.Class))
    vocabulary.lookup[0].remove((EX.Other, RDF.type, OWL.Class))
    vocabulary.graph().remove((EX.Image, RDF.type, OWL.Class))
    assert check_vocabulary(
        vocabulary, [TermDeclaration(str(EX.Image)), TermDeclaration(str(EX.Other))]
    ).verified
    blank = BNode("same-source-label")
    first = Graph().add((EX.one, EX.link, blank))
    second = Graph().add((EX.two, EX.link, blank))
    assert (
        len(set(Vocabulary(first, lookup=[second]).graph().objects(None, EX.link))) == 2
    )


@pytest.mark.parametrize("syntax", ["trig", "nquads"])
def test_datasets_preserve_graph_identity_and_require_selection(syntax: str) -> None:
    data = (
        "<https://example.org/default> <https://example.org/p> _:shared .\n"
        '<https://example.org/g> { _:shared <https://example.org/p> "named" . }'
    )
    if syntax == "nquads":
        data = (
            "<https://example.org/default> <https://example.org/p> _:shared .\n"
            '_:shared <https://example.org/p> "named" <https://example.org/g> .'
        )
    prepared = prepare_source(data, format=syntax)
    with pytest.raises(InputError, match="graph name"):
        prepared.graph()
    assert len(prepared.graph("@default")) == 1
    assert len(prepared.graph(str(EX.g))) == 1
    exported = prepared.dataset()
    shared = next(exported.default_graph.objects())
    assert (shared, EX.p, Literal("named", datatype=XSD.string)) in exported.graph(EX.g)
    assert len(prepared.graph(union=True)) == 2
    exported.graph(EX.g).remove((None, None, None))
    assert len(prepared.graph(str(EX.g))) == 1
    assert len(prepare_source(exported).graph(str(EX.g))) == 0
    dataset = Dataset()
    dataset.graph(EX.g).add((EX.Image, RDF.type, OWL.Class))
    with pytest.raises(InputError, match="graph name"):
        load_vocabulary(dataset)
    assert check_vocabulary(
        load_vocabulary(dataset, graph_name=str(EX.g)), [TermDeclaration(str(EX.Image))]
    ).verified
    with pytest.warns(DeprecationWarning):
        legacy = ConjunctiveGraph()
    legacy.get_context(EX.g).add((EX.Image, RDF.type, OWL.Class))
    with pytest.raises(InputError, match="graph name"):
        load_vocabulary(legacy)


def test_inferred_self_import_and_explicit_effective_bases(tmp_path: Path) -> None:
    path = tmp_path / "ontology.ttl"
    path.write_text(
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
        "<> a owl:Ontology; owl:imports <>. <#Image> a owl:Class."
    )
    vocabulary = load_vocabulary(path)
    assert len(vocabulary.imports) == 0
    assert check_vocabulary(
        vocabulary, [TermDeclaration(path.resolve().as_uri() + "#Image")]
    ).verified
    aliased = prepare_source(
        RdfSource(
            "<#Image> a <http://www.w3.org/2002/07/owl#Class>.",
            format="turtle",
            document_iri="urn:logical:ontology",
            effective_url="https://example.org/actual.ttl",
        )
    )
    assert (
        URIRef("https://example.org/actual.ttl#Image"),
        RDF.type,
        OWL.Class,
    ) in aliased.graph()


def test_size_triples_and_total_resource_bounds_apply_before_success(
    tmp_path: Path,
) -> None:
    with pytest.raises(LimitExceeded):
        prepare_source(
            BytesIO(TURTLE.encode()), format="turtle", limits=Limits(max_bytes=8)
        )
    graph = Graph().add((EX.a, EX.p, Literal("é" * 10)))
    with pytest.raises(LimitExceeded):
        prepare_source(graph, limits=Limits(max_bytes=20))
    root = tmp_path / "root.ttl"
    root.write_text(
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. <> owl:imports <https://example.org/import.ttl>."
    )
    imported = RdfSource(
        TURTLE, format="turtle", document_iri="https://example.org/import.ttl"
    )
    with pytest.raises(LimitExceeded, match="max_resources"):
        load_vocabulary(
            root,
            imports={"https://example.org/import.ttl": imported},
            limits=Limits(max_resources=1),
        )
    with pytest.raises(LimitExceeded, match="max_total_bytes"):
        load_vocabulary(
            root,
            imports={"https://example.org/import.ttl": imported},
            limits=Limits(max_total_bytes=120),
        )


def test_owned_exports_preserve_blank_identifiers_without_allowing_mutation() -> None:
    name, record = BNode("graph"), BNode("record")
    dataset = Dataset()
    dataset.graph(name).add((record, EX.name, Literal("record")))
    dataset.bind("example", EX)
    prepared = prepare_source(dataset)
    exported = prepared.dataset()
    selected_name = next(
        graph.identifier
        for graph in exported.graphs()
        if isinstance(graph.identifier, BNode)
    )
    first = prepared.graph(selected_name)
    second = prepared.graph(selected_name)
    assert set(first) == set(second) == set(exported.graph(selected_name))
    assert str(dict(exported.namespaces())["example"]) == str(EX)
    assert str(dict(first.namespaces())["example"]) == str(EX)
    first.remove((None, None, None))
    assert len(prepared.graph(selected_name)) == 1
    view = Vocabulary(second)
    assert set(view.local) == set(view.graph())
    view.local.remove((None, None, None))
    assert len(view.graph()) == 1


def test_required_aliases_reject_conflicting_requested_sources_without_reading_unused(
    tmp_path: Path,
) -> None:
    root = Graph().add((EX.Root, OWL.imports, EX.A))
    root.add((EX.Root, OWL.imports, EX.B))
    first = RdfSource(
        "<urn:class:A> a <http://www.w3.org/2002/07/owl#Class> .",
        format="turtle",
        document_iri=str(EX.A),
        effective_url=str(EX.B),
    )
    different = RdfSource(
        "<urn:class:B> a <http://www.w3.org/2002/07/owl#Class> .", format="turtle"
    )
    with pytest.raises(ResourceError, match="alias"):
        load_vocabulary(root, imports={str(EX.A): first, str(EX.B): different})
    same = RdfSource(first.content, format="turtle")
    view = load_vocabulary(
        root,
        imports={
            str(EX.A): first,
            str(EX.B): same,
            str(EX.Unused): tmp_path / "missing.ttl",
        },
    )
    assert len(view.imports) == 1
    assert (URIRef("urn:class:A"), RDF.type, OWL.Class) in view.imports[0]
    relative = Graph().add((EX.Root, OWL.imports, EX.Document))
    view = load_vocabulary(
        relative,
        imports={
            str(EX.Document): RdfSource(
                "<> a <http://www.w3.org/2002/07/owl#Ontology> .", format="turtle"
            )
        },
    )
    assert (EX.Document, RDF.type, OWL.Ontology) in view.imports[0]


@pytest.mark.parametrize("owned_worker", (False, True))
def test_required_graph_aliases_use_blank_node_aware_agreement(
    owned_worker: bool,
) -> None:
    root = Graph().add((EX.Root, OWL.imports, EX.A))
    root.add((EX.Root, OWL.imports, EX.B))
    left = Graph().add((EX.Class, EX.metadata, BNode("left")))
    right = Graph().add((EX.Class, EX.metadata, BNode("right")))
    view = load_vocabulary(
        root,
        imports={str(EX.A): RdfSource(left, effective_url=str(EX.B)), str(EX.B): right},
        _owned_worker=owned_worker,
    )
    assert len(view.imports) == 1
    changed = Graph().add((EX.OtherClass, EX.metadata, BNode("right")))
    with pytest.raises(ResourceError, match="alias"):
        load_vocabulary(
            root,
            imports={
                str(EX.A): RdfSource(left, effective_url=str(EX.B)),
                str(EX.B): changed,
            },
            _owned_worker=owned_worker,
        )


@pytest.mark.parametrize("role", ["import", "lookup"])
def test_resource_graph_selections_exclude_unrelated_declarations_and_imports(
    role: str,
) -> None:
    from rdflib.namespace import RDFS

    root = Graph().add((EX.Root, RDFS.subClassOf, EX.Image))
    source = RdfSource(
        "@prefix ex:<https://example.org/>. "
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
        "ex:ontology { ex:Image a owl:Class. } "
        "ex:application { ex:Poison a owl:Class; owl:imports ex:Unsupplied. "
        "ex:Image a owl:DatatypeProperty. }",
        format="trig",
        graph_name=str(EX.ontology),
    )
    if role == "import":
        root.add((EX.Root, OWL.imports, EX.Document))
        view = load_vocabulary(root, imports={str(EX.Document): source})
    else:
        view = load_vocabulary(root, lookup=[source])
    assert check_vocabulary(
        view, [TermDeclaration(str(EX.Image), (str(OWL.Class),))], check_references=True
    ).verified
    absent = check_vocabulary(view, [TermDeclaration(str(EX.Poison))])
    assert [issue.code for issue in absent.issues] == ["vocabulary.term_missing"]
    unselected = RdfSource(source.content, format="trig")
    with pytest.raises(InputError, match="graph name"):
        if role == "import":
            load_vocabulary(root, imports={str(EX.Document): unselected})
        else:
            load_vocabulary(root, lookup=[unselected])
    with pytest.raises(InputError, match="absent"):
        load_vocabulary(
            Graph(),
            lookup=[
                RdfSource(source.content, format="trig", graph_name=str(EX.absent))
            ],
        )


@pytest.mark.parametrize("selection", ["@default", str(EX.ontology), "union"])
def test_dataset_resource_bindings_survive_reparsing_and_import_cycles(
    selection: str,
) -> None:
    root = RdfSource(
        Graph().add((EX.Root, OWL.imports, EX.Document)), document_iri=str(EX.Root)
    )
    imported = RdfSource(
        "@prefix ex:<https://example.org/>. "
        "@prefix owl:<http://www.w3.org/2002/07/owl#>. "
        "ex:Default a owl:Class. "
        "ex:ontology { ex:Image a owl:Class. ex:Document owl:imports ex:Root. }",
        format="trig",
        graph_name=None if selection == "union" else selection,
        union=selection == "union",
    )
    expected = {EX.Default} if selection == "@default" else {EX.Image}
    if selection == "union":
        expected.add(EX.Default)
    for _ in range(2):
        view = load_vocabulary(root, imports={str(EX.Document): imported})
        assert len(view.imports) == 1
        assert set(view.imports[0].subjects(RDF.type, OWL.Class)) == expected


def test_import_alias_agreement_uses_selected_content_without_hidden_graphs() -> None:
    from dataclasses import replace

    root = Graph().add((EX.Root, OWL.imports, EX.A))
    root.add((EX.Root, OWL.imports, EX.B))
    source = RdfSource(
        "@prefix ex:<https://example.org/>. "
        "ex:ontology { ex:Image ex:p ex:o. } "
        "ex:application { ex:Other ex:p ex:o. }",
        format="trig",
        graph_name=str(EX.ontology),
        effective_url=str(EX.B),
    )
    different_selection = replace(source, graph_name=str(EX.application))
    with pytest.raises(ResourceError, match="alias"):
        load_vocabulary(
            root, imports={str(EX.A): source, str(EX.B): different_selection}
        )
    changed_hidden = replace(
        source, content=str(source.content).replace("ex:Other", "ex:Changed")
    )
    view = load_vocabulary(root, imports={str(EX.A): source, str(EX.B): changed_hidden})
    assert len(view.imports) == 1
    assert set(view.imports[0]) == {(EX.Image, EX.p, EX.o)}


def test_import_qualification_retains_loader_provenance_and_actual_aliases() -> None:
    root = Graph().add((EX.Root, OWL.imports, EX.Requested))
    supplied = RdfSource(
        "<urn:imported:Class> a <http://www.w3.org/2002/07/owl#Class> .",
        format="turtle",
        document_iri=str(EX.Logical),
        effective_url=str(EX.Actual),
    )
    assert not Vocabulary(root).required_imports_qualified
    assert not Vocabulary(root, imports=[Graph()]).required_imports_qualified
    with pytest.raises(ResourceError):
        load_vocabulary(root)
    view = load_vocabulary(root, imports={str(EX.Requested): supplied})
    assert view.required_imports_qualified
    assert (URIRef("urn:imported:Class"), RDF.type, OWL.Class) in view.graph()
    view.graph().remove((None, None, None))
    assert view.required_imports_qualified
    assert len(view.graph()) == 2
    assert load_vocabulary(Graph()).required_imports_qualified
