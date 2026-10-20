"""Actual maintained DOSDP generation and inverse finite assignment coverage."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from threading import Event

import pytest
from bijux_ontology_reasoning.authoring.patterns import (
    DosdpPattern,
    DosdpQueryBackend,
    PatternFiller,
    PatternInstance,
    check_patterns,
    prepare_dosdp_backend,
    prepare_dosdp_pattern,
)
from rdflib import Dataset, Graph, URIRef
from rdflib.namespace import OWL

from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Budget, Limits
from bijux_ontology.rdf.queries import QueryScope
from bijux_ontology.rdf.sources.types import RdfSource

_PREFIXES = {"ex": "urn:pattern:", "owl": str(OWL)}
_SCOPE = QueryScope("default")


def _data() -> dict[str, object]:
    """Supply the original native genus and two named restriction pattern."""
    return {
        "pattern_name": "genus_two_restrictions",
        "pattern_iri": "urn:pattern:two",
        "classes": {"genus": "ex:G", "thing": "owl:Thing"},
        "relations": {"p": "ex:p", "q": "ex:q"},
        "vars": {"first": "'thing'", "second": "'thing'"},
        "equivalentTo": {
            "text": "'genus' and ('p' some %s) and ('q' some %s)",
            "vars": ["first", "second"],
        },
    }


def _pattern() -> DosdpPattern:
    """Capture the exact supported JSON subset through the public factory."""
    return prepare_dosdp_pattern(json.dumps(_data()), prefixes=_PREFIXES)


def _expected(first: str = "A") -> PatternInstance:
    """Declare an exact complete assignment independently of query execution."""
    return PatternInstance(
        "urn:pattern:two",
        URIRef("urn:pattern:C"),
        (
            PatternFiller("first", URIRef("urn:pattern:" + first)),
            PatternFiller("second", URIRef("urn:pattern:B")),
        ),
    )


@pytest.fixture(scope="module")
def backend(tmp_path_factory: pytest.TempPathFactory) -> DosdpQueryBackend:
    """Require a real explicit maintained release; never download during tests."""
    java, bundle, pin = (
        os.environ.get(name)
        for name in (
            "BIJUX_DOSDP_JAVA",
            "BIJUX_DOSDP_BUNDLE",
            "BIJUX_DOSDP_MANIFEST_SHA256",
        )
    )
    if not (java and bundle and pin):
        pytest.skip(
            "An explicit pinned maintained DOSDP distribution is not configured"
        )
    return prepare_dosdp_backend(
        Path(java),
        Path(bundle),
        manifest_sha256=pin,
        artifacts=tmp_path_factory.mktemp("native-pattern-jobs"),
        limits=Limits(max_total_bytes=200_000_000),
    )


@pytest.fixture(scope="module")
def generated(
    backend: DosdpQueryBackend, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Use actual DOSDP generation and its maintained OWLAPI RDF writer.

    The tiny producer adapter performs only native parse/serialize. It contains
    no replacement axioms, matching logic, validation verdicts or expected rows.
    A JDK is needed solely for this real producer fixture, not for the API.
    """
    javac = backend.java.with_name("javac")
    if not javac.is_file():
        pytest.skip("The actual native producer fixture requires an explicit local JDK")
    directory = tmp_path_factory.mktemp("native-pattern-producer")
    pattern, prefixes, table, owl, rdf = (
        directory / name
        for name in (
            "pattern.json",
            "prefixes.json",
            "fillers.tsv",
            "generated.ofn",
            "generated.rdf",
        )
    )
    pattern.write_text(json.dumps(_data()), encoding="utf-8")
    prefixes.write_text(json.dumps(_PREFIXES), encoding="utf-8")
    table.write_text(
        "defined_class\tfirst\tsecond\nex:C\tex:A\tex:B\nex:C\tex:A2\tex:B\n",
        encoding="utf-8",
    )
    classpath = os.pathsep.join(str(backend.directory / jar) for jar in backend.jars)
    subprocess.run(
        [
            str(backend.java),
            "-Xmx256m",
            "-cp",
            classpath,
            "org.monarchinitiative.dosdp.cli.Main",
            "generate",
            "--template=" + str(pattern),
            "--prefixes=" + str(prefixes),
            "--infile=" + str(table),
            "--outfile=" + str(owl),
            "--restrict-axioms-to=logical",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    source = directory / "NativeRdfExport.java"
    source.write_text(
        """
import java.io.File;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.RDFXMLDocumentFormat;
import org.semanticweb.owlapi.model.IRI;
public class NativeRdfExport {
 public static void main(String[] args) throws Exception {
  var manager=OWLManager.createOWLOntologyManager();
  var ontology=manager.loadOntologyFromOntologyDocument(new File(args[0]));
  var format=new RDFXMLDocumentFormat();
  format.setDefaultPrefix("urn:pattern:");
  manager.saveOntology(ontology,format,IRI.create(new File(args[1])));
 }
}
""",
        encoding="utf-8",
    )
    subprocess.run(
        [str(javac), "-cp", classpath, str(source)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [
            str(backend.java),
            "-Xmx256m",
            "-cp",
            str(directory) + os.pathsep + classpath,
            "NativeRdfExport",
            str(owl),
            str(rdf),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return rdf


def _graph(generated: Path) -> Graph:
    """Read the actual native producer output independently for each control."""
    return Graph().parse(generated, format="xml")


def test_actual_generator_fillers_and_extra_policy(
    backend: DosdpQueryBackend, generated: Path
) -> None:
    source = prepare_source(_graph(generated))
    result = check_patterns(
        backend, source, (_pattern(),), (_expected(), _expected("A2")), scope=_SCOPE
    )
    assert result.report.verified
    query = result.queries[0]
    assert set(query.matches) == {_expected(), _expected("A2")}
    assert (
        query.native_identity is not None and "version: 0.20.0" in query.native_identity
    )
    assert query.query is not None and "owl:someValuesFrom ?first" in query.query
    assert query.engine is not None and query.engine.answer is not None
    assert (
        query.engine.config.engine == "rdflib"
        and query.engine.answer.external_complete is True
    )
    assert len(query.engine.answer.rows) == 2
    extra = check_patterns(backend, source, (_pattern(),), (_expected(),), scope=_SCOPE)
    assert extra.report.complete and not extra.report.verified
    assert any(
        issue.code == "pattern.unexpected_binding" and "A2" in (issue.actual or "")
        for issue in extra.report.issues
    )
    allowed = check_patterns(
        backend, source, (_pattern(),), (_expected(),), scope=_SCOPE, extras="allow"
    )
    assert allowed.report.verified and len(allowed.queries[0].matches) == 2


def test_changed_required_property_and_filler_are_located(
    backend: DosdpQueryBackend, generated: Path
) -> None:
    graph = _graph(generated)
    for restriction in tuple(graph.subjects(OWL.onProperty, URIRef("urn:pattern:p"))):
        graph.remove((restriction, OWL.onProperty, URIRef("urn:pattern:p")))
        graph.add((restriction, OWL.onProperty, URIRef("urn:pattern:other")))
    changed = check_patterns(
        backend, prepare_source(graph), (_pattern(),), (_expected(),), scope=_SCOPE
    )
    assert changed.report.complete and not changed.report.verified
    assert not changed.queries[0].matches
    assert any(
        issue.code == "pattern.expected_missing" and issue.location == "urn:pattern:C"
        for issue in changed.report.issues
    )
    mismatch = check_patterns(
        backend,
        prepare_source(_graph(generated)),
        (_pattern(),),
        (_expected("absent"),),
        scope=_SCOPE,
    )
    assert any(
        issue.code == "pattern.expected_missing" and "absent" in (issue.expected or "")
        for issue in mismatch.report.issues
    )


def test_unknown_profiles_missing_patterns_and_input_contracts(
    backend: DosdpQueryBackend, generated: Path, tmp_path: Path
) -> None:
    source = prepare_source(_graph(generated))
    data = _data()
    data["vars"] = {"first": "'genus'", "second": "'thing'"}
    pattern = prepare_dosdp_pattern(json.dumps(data), prefixes=_PREFIXES)
    unsupported = check_patterns(
        backend, source, (pattern,), (_expected(),), scope=_SCOPE
    )
    assert not unsupported.report.verified and unsupported.queries[0].engine is None
    inferred = check_patterns(
        backend,
        source,
        (_pattern(),),
        (_expected(),),
        scope=_SCOPE,
        matching="inferred",
    )
    assert inferred.report.outcome == "unverified"
    assert not check_patterns(backend, source, (), (), scope=_SCOPE).report.verified
    with pytest.raises(ResourceError, match="unavailable"):
        prepare_dosdp_pattern(tmp_path / "https:missing-pattern", prefixes=_PREFIXES)
    missing = PatternInstance(
        "urn:pattern:absent", _expected().defined_class, _expected().fillers
    )
    assert not check_patterns(
        backend, source, (_pattern(),), (missing,), scope=_SCOPE
    ).report.verified
    incomplete = PatternInstance("urn:pattern:two", _expected().defined_class, ())
    assert not check_patterns(
        backend, source, (_pattern(),), (incomplete,), scope=_SCOPE
    ).report.verified
    with pytest.raises(InputError):
        DosdpPattern()


def test_owned_local_import_closure_and_unrequested_exclusion(
    backend: DosdpQueryBackend, generated: Path
) -> None:
    graph = Graph()
    graph.add((URIRef("urn:pattern:root"), OWL.imports, URIRef("urn:pattern:import")))
    source = prepare_source(RdfSource(graph, document_iri="urn:pattern:root"))
    imports = {"urn:pattern:import": RdfSource(_graph(generated))}
    result = check_patterns(
        backend,
        source,
        (_pattern(),),
        (_expected(), _expected("A2")),
        scope=_SCOPE,
        imports=imports,
    )
    assert result.report.verified and len(result.import_metadata) == 1
    missing = check_patterns(
        backend, source, (_pattern(),), (_expected(),), scope=_SCOPE
    )
    assert missing.report.outcome == "unverified" and not missing.queries
    unrelated = check_patterns(
        backend,
        prepare_source(Graph()),
        (_pattern(),),
        (_expected(),),
        scope=_SCOPE,
        imports={"urn:pattern:unused": RdfSource(_graph(generated))},
    )
    assert unrelated.report.complete and not unrelated.queries[0].matches
    assert not unrelated.import_metadata


def test_physical_scope_and_result_truncation(
    backend: DosdpQueryBackend, generated: Path
) -> None:
    dataset = Dataset()
    selected = dataset.graph(URIRef("urn:pattern:selected"))
    for triple in _graph(generated):
        selected.add(triple)
    source = prepare_source(dataset)
    scope = QueryScope("named", "urn:pattern:selected")
    result = check_patterns(
        backend, source, (_pattern(),), (_expected(), _expected("A2")), scope=scope
    )
    assert result.report.verified and result.scope == scope
    default = check_patterns(
        backend, source, (_pattern(),), (_expected(),), scope=_SCOPE
    )
    assert default.report.complete and not default.queries[0].matches
    truncated = check_patterns(
        backend, source, (_pattern(),), (_expected(),), scope=scope, max_rows=1
    )
    assert not truncated.report.verified and not truncated.report.complete


def test_shared_bounds_cancel_and_recovery(
    backend: DosdpQueryBackend, generated: Path
) -> None:
    source = prepare_source(_graph(generated))
    for limits in (
        Limits(max_total_bytes=128),
        Limits(max_work=4),
        Limits(max_states=4),
        Limits(max_triples=4),
    ):
        budget = Budget(limits)
        result = check_patterns(
            backend, source, (_pattern(),), (_expected(),), scope=_SCOPE, budget=budget
        )
        assert not result.report.verified and any(
            issue.kind == "exhausted" for issue in result.report.issues
        )
        assert all(
            budget.remaining(kind) >= 0
            for kind in ("bytes", "work", "states", "triples")
        )
    cancelled = Event()
    cancelled.set()
    result = check_patterns(
        backend, source, (_pattern(),), (_expected(),), scope=_SCOPE, cancel=cancelled
    )
    assert result.report.outcome == "exhausted"
    recovered = check_patterns(
        backend, source, (_pattern(),), (_expected(), _expected("A2")), scope=_SCOPE
    )
    assert recovered.report.verified
