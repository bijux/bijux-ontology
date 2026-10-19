"""Offline catalog bindings preserve logical identities and vocabulary roles."""

from dataclasses import replace
from pathlib import Path

import pytest
from rdflib import Graph, Namespace
from rdflib.namespace import OWL, RDF

from bijux_ontology.declarations import TermDeclaration
from bijux_ontology.diagnostics.errors import InputError, LimitExceeded, ResourceError
from bijux_ontology.limits import Limits
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.vocabulary.checking import check_vocabulary
from bijux_ontology.vocabulary.sources.catalogs import OntologyVersion, load_catalog
from bijux_ontology.vocabulary.sources.loading import load_vocabulary

EX = Namespace("https://catalog.example/")
XMLNS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"
OWL_PREFIX = "@prefix owl:<http://www.w3.org/2002/07/owl#>. "


def _catalog(path: Path, body: str, *, attributes: str = "") -> Path:
    """Write a conventional namespaced catalog around the requested entries."""
    path.write_text(f'<catalog xmlns="{XMLNS}" {attributes}>{body}</catalog>')
    return path


def test_transitive_selected_version_preserves_bases_and_owned_bytes(
    tmp_path: Path,
) -> None:
    resources = tmp_path / "resources"
    resources.mkdir()
    (resources / "chosen.ttl").write_text(
        OWL_PREFIX + "<> owl:imports <dependency> . <#Chosen> a owl:Class."
    )
    (resources / "dependency.ttl").write_text(
        OWL_PREFIX + "<> owl:imports <https://catalog.example/module> . "
        "<#Dependency> a owl:Class."
    )
    catalog = _catalog(
        tmp_path / "catalog.xml",
        '<group xml:base="resources/">'
        '<uri name="https://catalog.example/versions/chosen" uri="chosen.ttl"/>'
        '<uri name="https://catalog.example/versions/dependency" uri="dependency.ttl"/>'
        "</group>",
        attributes='prefer="public"',
    )
    bindings = load_catalog(
        catalog,
        versions=[OntologyVersion(str(EX.module), str(EX.versions) + "/chosen")],
    )
    assert bindings[str(EX.module)] is bindings[str(EX.versions) + "/chosen"]
    assert bindings[str(EX.module)].document_iri == str(EX.versions) + "/chosen"
    with pytest.raises(TypeError):
        bindings[str(EX.module)] = RdfSource("")  # type: ignore[index]
    (resources / "chosen.ttl").unlink()
    root = RdfSource(
        OWL_PREFIX + "<> owl:imports <https://catalog.example/module>. "
        "<#Root> a owl:Class.",
        format="turtle",
        document_iri=str(EX.root),
    )
    vocabulary = load_vocabulary(root, imports=bindings)
    assert len(vocabulary.imports) == 2
    assert check_vocabulary(
        vocabulary,
        [
            TermDeclaration(str(EX.root) + "#Root"),
            TermDeclaration(str(EX.versions) + "/chosen#Chosen"),
            TermDeclaration(str(EX.versions) + "/dependency#Dependency"),
        ],
    ).verified
    assert len(vocabulary.local) == 2


def test_catalog_entries_remain_unassigned_until_imported_or_used_as_lookup(
    tmp_path: Path,
) -> None:
    (tmp_path / "import.ttl").write_text(OWL_PREFIX + "<#Imported> a owl:Class.")
    (tmp_path / "lookup.ttl").write_text(OWL_PREFIX + "<#Lookup> a owl:Class.")
    catalog = _catalog(
        tmp_path / "catalog.xml",
        '<uri name="https://catalog.example/import" uri="import.ttl"/>'
        '<uri name="https://catalog.example/lookup" uri="lookup.ttl"/>',
    )
    bindings = load_catalog(catalog)
    root = Graph().add((EX.Root, RDF.type, OWL.Class))
    root.add((EX.root, OWL.imports, EX["import"]))
    vocabulary = load_vocabulary(
        root, imports=bindings, lookup=[bindings[str(EX.lookup)]]
    )
    assert len(vocabulary.imports) == 1
    assert len(vocabulary.lookup) == 1
    assert (EX["import#Imported"], RDF.type, OWL.Class) not in vocabulary.local
    assert check_vocabulary(
        vocabulary,
        [TermDeclaration(str(EX.lookup) + "#Lookup")],
    ).verified
    missing = Graph().add((EX.root, OWL.imports, EX.missing))
    with pytest.raises(ResourceError, match="no supplied source"):
        load_vocabulary(missing, imports=bindings, lookup=[bindings[str(EX.lookup)]])
    with pytest.raises(ResourceError, match="no supplied source"):
        load_vocabulary(root, lookup=[bindings[str(EX["import"])]])


def test_exact_names_base_scopes_and_explicit_dataset_selection(tmp_path: Path) -> None:
    folder = tmp_path / "with space"
    folder.mkdir()
    (folder / "data.trig").write_text(
        "<https://catalog.example/g> { <#Named> a "
        "<http://www.w3.org/2002/07/owl#Class>. }"
    )
    bindings = load_catalog(
        _catalog(
            tmp_path / "catalog.xml",
            '<group xml:base="ignored/">'
            '<uri xml:base="../with%20space/" '
            'name="https://catalog.example/ontology" uri="data.trig"/>'
            "</group>",
        )
    )
    source = bindings[str(EX.ontology)]
    with pytest.raises(InputError, match="graph name"):
        load_vocabulary(source)
    assert check_vocabulary(
        load_vocabulary(replace(source, graph_name=str(EX.g))),
        [TermDeclaration(str(EX.ontology) + "#Named")],
    ).verified
    root = Graph().add((EX.root, OWL.imports, EX["Ontology"]))
    with pytest.raises(ResourceError, match="no supplied source"):
        load_vocabulary(root, imports=bindings)


def test_selected_version_conflicts_do_not_depend_on_entry_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "old.ttl").write_text(OWL_PREFIX + "<#Old> a owl:Class.")
    (tmp_path / "new.ttl").write_text(OWL_PREFIX + "<#New> a owl:Class.")
    old = '<uri name="https://catalog.example/ontology" uri="old.ttl"/>'
    new = '<uri name="https://catalog.example/version" uri="new.ttl"/>'
    choice = OntologyVersion(str(EX.ontology), str(EX.version))
    for entries in [old + new, new + old]:
        catalog = _catalog(tmp_path / "catalog.xml", entries)
        with pytest.raises(InputError, match="conflicts with selected version"):
            load_catalog(catalog, versions=[choice])
    catalog = _catalog(tmp_path / "catalog.xml", new)
    with pytest.raises(InputError, match="duplicate"):
        load_catalog(catalog, versions=[choice, choice])
    with pytest.raises(ResourceError, match="no catalog entry"):
        load_catalog(
            catalog, versions=[OntologyVersion(str(EX.ontology), str(EX.missing))]
        )
    bindings = load_catalog(catalog, versions=[choice])
    assert bindings[str(EX.ontology)] is bindings[str(EX.version)]
    agreeing = old.replace("old.ttl", "new.ttl") + new
    assert load_catalog(_catalog(catalog, agreeing), versions=[choice])[
        str(EX.ontology)
    ]
    with pytest.raises(InputError, match="syntax conflicts"):
        load_catalog(catalog, versions=[choice], formats={str(EX.version): "xml"})


@pytest.mark.parametrize(
    "body,match",
    [
        (
            '<rewriteURI uriStartString="https://catalog.example/" '
            'rewritePrefix="./"/>',
            "Unsupported",
        ),
        ('<nextCatalog catalog="other.xml"/>', "Unsupported"),
        ('<system systemId="https://catalog.example/x" uri="x.ttl"/>', "Unsupported"),
        ('<uri name="https://catalog.example/x" uri="x.ttl"/>' * 2, "Duplicate"),
        ('<uri name="relative" uri="x.ttl"/>', "absolute IRI"),
        ('<uri name="https://catalog.example/x"/>', "requires"),
        (
            '<uri name="https://catalog.example/x" uri="https://remote.invalid/x.ttl"/>',
            "local file",
        ),
        ('<uri name="https://catalog.example/x" uri="x.ttl#part"/>', "local file"),
        ('<group unsupported="true"/>', "attribute"),
        ("<group>content</group>", "text"),
        ("<group><catalog/></group>", "nesting"),
    ],
)
def test_unsupported_and_ambiguous_catalogs_fail_before_resolution(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(InputError, match=match):
        load_catalog(_catalog(tmp_path / "catalog.xml", body))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtds_and_entities_are_rejected_by_parser(
    tmp_path: Path, encoding: str
) -> None:
    catalog = tmp_path / "catalog.xml"
    xml = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE catalog [<!ENTITY file SYSTEM "https://remote.invalid/entity">]>'
        f'<catalog xmlns="{XMLNS}">&file;</catalog>'
    )
    catalog.write_bytes(xml.encode(encoding))
    with pytest.raises(InputError, match="DTDs"):
        load_catalog(catalog)
    catalog.write_text(
        '<!DOCTYPE catalog SYSTEM "https://remote.invalid/catalog.dtd">'
        f'<catalog xmlns="{XMLNS}"/>'
    )
    with pytest.raises(InputError, match="DTDs"):
        load_catalog(catalog)


def test_missing_malformed_and_bounded_inputs(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.xml"
    with pytest.raises(ResourceError, match="Missing"):
        load_catalog(catalog)
    catalog.write_text(f'<catalog xmlns="{XMLNS}"><group></catalog>')
    with pytest.raises(InputError, match="Malformed XML"):
        load_catalog(catalog)
    _catalog(catalog, '<uri name="https://catalog.example/x" uri="missing.ttl"/>')
    with pytest.raises(ResourceError, match="Missing"):
        load_catalog(catalog)
    _catalog(catalog, "<group><group/></group>")
    with pytest.raises(LimitExceeded, match="max_depth"):
        load_catalog(catalog, limits=Limits(max_depth=2))
    with pytest.raises(LimitExceeded, match="max_bytes"):
        load_catalog(catalog, limits=Limits(max_bytes=10))
    with pytest.raises(LimitExceeded, match="max_work"):
        load_catalog(catalog, limits=Limits(max_work=1))
    with pytest.raises(LimitExceeded, match="max_states"):
        load_catalog(catalog, limits=Limits(max_states=1))
    target = tmp_path / "data.ttl"
    target.write_text(" " * 1000)
    _catalog(catalog, '<uri name="https://catalog.example/x" uri="data.ttl"/>')
    with pytest.raises(LimitExceeded, match="max_bytes"):
        load_catalog(catalog, limits=Limits(max_bytes=500))
    with pytest.raises(LimitExceeded, match="max_total_bytes"):
        load_catalog(catalog, limits=Limits(max_total_bytes=500))
    with pytest.raises(LimitExceeded, match="max_resources"):
        load_catalog(catalog, limits=Limits(max_resources=1))
    assert load_catalog(catalog, limits=Limits(max_resources=2))


def test_explicit_ambiguous_syntax_and_normal_import_alias_agreement(
    tmp_path: Path,
) -> None:
    target = tmp_path / "ontology.owl"
    target.write_text(OWL_PREFIX + "<#Selected> a owl:Class.")
    catalog = _catalog(
        tmp_path / "catalog.xml",
        '<uri name="https://catalog.example/version" uri="ontology.owl"/>',
    )
    choice = OntologyVersion(str(EX.ontology), str(EX.version))
    with pytest.raises(InputError, match="Unsupported RDF format"):
        load_catalog(catalog)
    with pytest.raises(InputError, match="no catalog entry"):
        load_catalog(catalog, formats={str(EX.missing): "turtle"})
    bindings = load_catalog(
        catalog, versions=[choice], formats={str(EX.version): "turtle"}
    )
    assert check_vocabulary(
        load_vocabulary(bindings[str(EX.ontology)]),
        [TermDeclaration(str(EX.version) + "#Selected")],
    ).verified
    conflicting = RdfSource(
        OWL_PREFIX + "<> owl:imports <https://catalog.example/ontology>. "
        "<#Other> a owl:Class.",
        format="turtle",
        document_iri=str(EX.version),
    )
    with pytest.raises(
        ResourceError, match="Conflicting or unverified supplied sources"
    ):
        load_vocabulary(conflicting, imports=bindings)
