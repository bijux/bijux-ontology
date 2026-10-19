"""Real pinned OWLAPI profile checks and honest optional native boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from bijux_ontology_reasoning.backend import (
    OwlProfileBackend,
    backend_capability,
    prepare_profile_backend,
)
from bijux_ontology_reasoning.profiles import check_profile
from rdflib import BNode, ConjunctiveGraph, Dataset, Graph, URIRef
from rdflib.namespace import OWL, RDF

from bijux_ontology.diagnostics.errors import LimitExceeded, ResourceError
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.rdf.terms import independent_blank_scopes

_PREFIX = """
@prefix : <urn:profile:> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:ontology a owl:Ontology .
"""
_SIMPLE = ":A a owl:Class . :B a owl:Class . :A rdfs:subClassOf :B ."
_UNION = """
:A a owl:Class . :B a owl:Class . :C a owl:Class .
:A owl:equivalentClass [a owl:Class; owl:unionOf (:B :C)] .
"""
_GLOBAL = """
:A a owl:Class . :p a owl:ObjectProperty, owl:TransitiveProperty .
:A rdfs:subClassOf [a owl:Restriction; owl:onProperty :p; owl:maxCardinality 1] .
"""


def _source(body: str, *, identity: str = "urn:source") -> PreparedSource:
    """Prepare original RDF independently of the optional native interpretation."""
    return prepare_rdf(
        RdfSource(_PREFIX + body, format="turtle", document_iri=identity)
    )


@pytest.fixture
def backend(tmp_path: Path) -> OwlProfileBackend:
    """Require an explicitly supplied built bundle; never discover or download it."""
    java, bundle, digest = (
        os.environ.get(name)
        for name in ("BIJUX_OWL_JAVA", "BIJUX_OWL_BUNDLE", "BIJUX_OWL_MANIFEST_SHA256")
    )
    if not all((java, bundle, digest)):
        pytest.skip("Explicit pinned OWL bundle and Java are not configured")
    assert java and bundle and digest
    return prepare_profile_backend(
        Path(java), Path(bundle), manifest_sha256=digest, artifacts=tmp_path / "jobs"
    )


def test_import_and_missing_runtime_are_explicit(tmp_path: Path) -> None:
    """Import needs no Java, and missing resources do not fabricate a capability."""
    environment = dict(os.environ, PATH="")
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", "import bijux_ontology_reasoning.profiles"],
        env=environment,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0
    node = BNode("same")
    first = Graph().add((node, RDF.type, OWL.NamedIndividual))
    shared = Graph().add((node, RDF.type, OWL.NamedIndividual))
    separate = Graph().add((BNode(str(node)), RDF.type, OWL.NamedIndividual))
    assert not independent_blank_scopes((first, shared), Budget())
    assert independent_blank_scopes((first, separate), Budget())
    with pytest.raises(LimitExceeded):
        independent_blank_scopes((Graph(), Graph()), Budget(Limits(max_work=1)))
    with pytest.raises(ResourceError):
        prepare_profile_backend(
            tmp_path / "missing-java",
            tmp_path / "missing-bundle",
            manifest_sha256="0" * 64,
        )


def test_actual_four_profiles_and_global_restriction(
    backend: OwlProfileBackend,
) -> None:
    """Independent profile classes preserve DL/EL distinctions and global findings."""
    capability = backend_capability(backend)
    assert capability.report.verified and capability.owlapi_version == "5.5.1"
    for profile in capability.profiles:
        checked = check_profile(backend, _source(_SIMPLE), profile=profile)
        assert checked.report.verified and checked.in_profile is True
        assert checked.loaders and all(item.qualified for item in checked.loaders)
    union = _source(_UNION)
    assert check_profile(backend, union, profile="owl2-dl").report.verified
    el = check_profile(backend, union, profile="owl2-el")
    assert el.in_profile is False and el.report.outcome == "disagreement"
    assert "UseOfIllegalClassExpression" in {item.kind for item in el.violations}
    global_result = check_profile(backend, _source(_GLOBAL), profile="owl2-dl")
    assert global_result.in_profile is False
    assert any("NonSimpleProperty" in item.kind for item in global_result.violations)
    assert all(item.document.document_iri == "urn:source" for item in el.violations)


def test_actual_parser_loss_cannot_accept_raw_positive(
    backend: OwlProfileBackend,
) -> None:
    """Typed unparsed-triple metadata blocks an actual maintained true answer."""
    source = _source('_:a a owl:AllDisjointClasses; owl:members "not a list" .')
    result = check_profile(backend, source, profile="owl2-dl")
    assert result.raw_in_profile is True and result.in_profile is None
    assert not result.report.verified and not result.report.complete
    assert any(item.unparsed and item.unparsed > 0 for item in result.loaders)
    assert "owl.loader_unverified" in {item.code for item in result.report.issues}


def test_offline_effective_import_alias_and_missing_closure(
    backend: OwlProfileBackend,
) -> None:
    """Actual imported violations retain source ownership without contacting HTTP."""
    contacts: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            contacts.append(self.path)
            self.send_error(503)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/import.ttl"
        root = _source(f"<urn:profile:ontology> owl:imports <{url}> .")
        imported = RdfSource(
            _PREFIX.replace("urn:profile:", "urn:import:") + _UNION,
            format="turtle",
            document_iri="urn:import:document",
            effective_url=url,
        )
        result = check_profile(
            backend, root, profile="owl2-el", imports={url: imported}
        )
        assert result.in_profile is False and len(result.documents) == 2
        assert all(item.qualified for item in result.loaders)
        assert any(
            item.document.document_iri == "urn:import:document"
            for item in result.violations
        )
        missing = check_profile(backend, root, profile="owl2-dl")
        assert missing.in_profile is None and not missing.report.verified
        assert contacts == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_physical_scope_and_bundle_revision(
    backend: OwlProfileBackend, tmp_path: Path
) -> None:
    """Pins cannot widen, and changed bundle bytes never run as the pinned worker."""
    text = _PREFIX + "<urn:good> { " + _SIMPLE + " } <urn:bad> { " + _GLOBAL + " }"
    source = prepare_rdf(RdfSource(text, format="trig", graph_name="urn:good"))
    result = check_profile(backend, source, profile="owl2-dl")
    assert result.report.verified and str(result.documents[0].graph_name) == "urn:good"
    unselected = prepare_rdf(RdfSource(text, format="trig"))
    assert check_profile(backend, unselected, profile="owl2-dl").in_profile is None
    clone = tmp_path / "bundle"
    shutil.copytree(backend.directory, clone)
    copied = prepare_profile_backend(
        backend.java,
        clone,
        manifest_sha256=backend.manifest_sha256,
        artifacts=tmp_path / "changed-jobs",
    )
    with (clone / copied.jars[0]).open("ab") as writer:
        writer.write(b"changed")
    assert not backend_capability(copied).report.verified
    with pytest.raises(ResourceError):
        prepare_profile_backend(
            backend.java, clone, manifest_sha256=hashlib.sha256(b"wrong").hexdigest()
        )


def test_owned_deadline_cancellation_and_paid_finding_prefix(
    backend: OwlProfileBackend,
) -> None:
    """Interrupted workers cannot accept; actual findings survive diagnostic caps."""
    source = _source(_GLOBAL)
    timeout = check_profile(backend, source, profile="owl2-dl", timeout=0.001)
    assert timeout.report.outcome == "exhausted"
    cancellation = threading.Event()
    timer = threading.Timer(0.15, cancellation.set)
    timer.start()
    started = time.monotonic()
    try:
        interrupted = check_profile(
            backend, source, profile="owl2-dl", cancel=cancellation
        )
    finally:
        timer.join()
    assert interrupted.report.outcome == "exhausted"
    assert time.monotonic() - started < 4
    prefix = check_profile(
        backend,
        _source(_UNION + ":B owl:equivalentClass [a owl:Class; owl:unionOf (:A :C)] ."),
        profile="owl2-el",
        limits=Limits(max_issues=1),
    )
    assert prefix.in_profile is False and prefix.report.outcome == "exhausted"
    assert any(item.code == "owl.profile_violation" for item in prefix.report.issues)
    assert check_profile(backend, _source(_SIMPLE), profile="owl2-dl").report.verified


def test_pinned_bundle_paths_cannot_split_the_java_classpath(
    backend: OwlProfileBackend, tmp_path: Path
) -> None:
    """Every pinned jar remains one classpath entry at preparation and use."""
    assert backend_capability(backend).report.verified
    separated = tmp_path / ("bundle" + os.pathsep + "entry")
    shutil.copytree(backend.directory, separated)
    with pytest.raises(ResourceError, match="classpath separator"):
        prepare_profile_backend(
            backend.java, separated, manifest_sha256=backend.manifest_sha256
        )
    clone = tmp_path / "bundle"
    shutil.copytree(backend.directory, clone)
    manifest_path = clone / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    entry = manifest["jars"][0]
    name = entry["name"]
    entry["name"] = "pinned" + os.pathsep + "entry.jar"
    (clone / name).rename(clone / entry["name"])
    raw = json.dumps(manifest).encode()
    manifest_path.write_bytes(raw)
    with pytest.raises(ResourceError, match="flat jar names"):
        prepare_profile_backend(
            backend.java, clone, manifest_sha256=hashlib.sha256(raw).hexdigest()
        )


def test_native_output_and_ambient_option_boundaries(
    backend: OwlProfileBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actual oversized native result exhausts; ambient JVM injection never runs."""
    source = _source(
        " ".join(f"<urn:C{i}> rdfs:subClassOf <urn:D{i}> ." for i in range(80))
    )
    classpath_bytes = sum(
        len(str(backend.directory / name).encode()) + 1 for name in backend.jars
    )
    result = check_profile(
        backend,
        source,
        profile="owl2-dl",
        limits=Limits(max_bytes=max(8000, classpath_bytes + 1)),
    )
    assert result.report.outcome == "exhausted" and result.in_profile is None
    assert any("OWL worker exhausted" in item.message for item in result.report.issues)
    monkeypatch.setenv("JAVA_TOOL_OPTIONS", "-Dunrequested=true")
    unavailable = backend_capability(backend)
    assert unavailable.java_version is None and not unavailable.report.verified


def test_original_blank_identity_required_aliases_and_physical_scopes(
    backend: OwlProfileBackend,
) -> None:
    """Only actual independently loaded original graph roles must be separable."""
    root_iri, import_iri = URIRef("urn:root"), URIRef("urn:import")
    a, b = URIRef("urn:A"), URIRef("urn:B")
    graph = (
        Graph()
        .add((root_iri, RDF.type, OWL.Ontology))
        .add((root_iri, OWL.imports, import_iri))
        .add((a, RDF.type, OWL.Class))
        .add((BNode(), RDF.type, a))
    )
    source = prepare_rdf(RdfSource(graph, document_iri=str(root_iri)))
    owned = next(source.graph().subjects(RDF.type, a))
    assert isinstance(owned, BNode)
    imported = (
        Graph()
        .add((import_iri, RDF.type, OWL.Ontology))
        .add((b, RDF.type, OWL.Class))
        .add((owned, RDF.type, b))
    )
    shared = check_profile(
        backend, source, profile="owl2-dl", imports={str(import_iri): imported}
    )
    assert shared.in_profile is None and not shared.report.verified
    assert not shared.loaders
    imported.remove((owned, RDF.type, b))
    imported.add((BNode(str(owned)), RDF.type, b))
    separate = check_profile(
        backend, source, profile="owl2-dl", imports={str(import_iri): imported}
    )
    assert separate.report.verified and separate.in_profile is True
    unused = Graph().add((owned, RDF.type, b))
    extra = check_profile(
        backend,
        source,
        profile="owl2-dl",
        imports={
            "urn:unused": RdfSource(unused, document_iri=str(import_iri)),
            str(import_iri): imported,
        },
    )
    assert extra.report.verified and len(extra.documents) == 2

    # Both requested aliases are core-qualified as one actual imported document.
    alias = "urn:alias"
    root_graph = source.graph()
    root_graph.add((root_iri, OWL.imports, URIRef(alias)))
    aliased_source = prepare_rdf(RdfSource(root_graph, document_iri=str(root_iri)))
    descriptor = RdfSource(imported, document_iri=str(import_iri), effective_url=alias)
    aliased_owned = next(aliased_source.graph().subjects(RDF.type, a))
    duplicate = Graph()
    for triple in imported:
        subject, predicate, value = triple
        duplicate.add(
            (aliased_owned if isinstance(subject, BNode) else subject, predicate, value)
        )
    assert any(node is aliased_owned for triple in duplicate for node in triple)
    aliased = check_profile(
        backend,
        aliased_source,
        profile="owl2-dl",
        imports={
            str(import_iri): RdfSource(duplicate, document_iri=str(import_iri)),
            alias: descriptor,
        },
    )
    assert aliased.report.verified and len(aliased.documents) == 2

    # Shared terms in an unselected partition cannot poison the selected role.
    dataset = Dataset()
    for triple in imported:
        dataset.graph(URIRef("urn:selected")).add(triple)
    dataset.graph(URIRef("urn:hidden")).add((owned, RDF.type, b))
    selected = RdfSource(
        dataset, document_iri=str(import_iri), graph_name="urn:selected"
    )
    scoped = check_profile(
        backend, source, profile="owl2-dl", imports={str(import_iri): selected}
    )
    assert scoped.report.verified and scoped.in_profile is True
    selected_graph = dataset.graph(URIRef("urn:selected"))
    separate_node = next(selected_graph.subjects(RDF.type, b))
    selected_graph.remove((separate_node, RDF.type, b)).add((owned, RDF.type, b))
    selected_shared = check_profile(
        backend, source, profile="owl2-dl", imports={str(import_iri): selected}
    )
    assert selected_shared.in_profile is None and not selected_shared.report.verified

    legacy = ConjunctiveGraph()
    for triple in imported:
        legacy.default_context.add(triple)
    legacy_source = RdfSource(
        legacy, document_iri=str(import_iri), graph_name="@default"
    )
    unsupported = check_profile(
        backend, source, profile="owl2-dl", imports={str(import_iri): legacy_source}
    )
    assert unsupported.in_profile is None and not unsupported.report.verified
