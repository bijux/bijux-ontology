"""The installed SDRF consumer keeps validation and exact table cell ownership."""

import io
import socket
from pathlib import Path
from typing import Any

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS

from bijux_ontology.interfaces.adapters.sdrf import (
    SdrfOntology,
    SdrfTerminology,
    check_sdrf,
    local_sdrf_client,
    prepare_sdrf_terminology,
)
from bijux_ontology.limits import Budget, Limits
from bijux_ontology.vocabulary.browsing.inspection import inspect_vocabulary
from bijux_ontology.vocabulary.sources.loading import load_vocabulary
from bijux_ontology.vocabulary.sources.view import Vocabulary

sdrf = pytest.importorskip("sdrf_pipelines.sdrf.sdrf")
models = pytest.importorskip("sdrf_pipelines.sdrf.schemas.models")
registry_module = pytest.importorskip("sdrf_pipelines.sdrf.schemas.registry")
validators = pytest.importorskip("sdrf_pipelines.sdrf.validators")
ols = pytest.importorskip("sdrf_pipelines.ols.ols")
E = Namespace("urn:sdrf:")
OIO = Namespace("http://www.geneontology.org/formats/oboInOwl#")
COLUMN = "characteristics[cell type]"


def _terminology(*, closed: bool = True, collision: bool = False) -> SdrfTerminology:
    """Use only explicitly supplied accession and lexical predicate mappings."""
    graph = Graph()
    for iri, accession, label in (
        (E.Root, "EX:Root", "Root"),
        (E.Child, "EX:Child", "Child"),
        (E.Other, "EX:Other", "Different"),
    ):
        graph.add((iri, RDF.type, OWL.Class))
        graph.add((iri, OIO.id, Literal(accession)))
        graph.add((iri, RDFS.label, Literal(label)))
    graph.add((E.Child, RDFS.subClassOf, E.Root))
    graph.add((E.Child, OIO.hasExactSynonym, Literal("Child synonym")))
    if collision:
        graph.add((E.Collision, OIO.id, Literal("ex:child")))
        graph.add((E.Collision, RDFS.label, Literal("Collision")))
        graph.add((E.Collision, RDFS.subClassOf, E.Root))
    return prepare_sdrf_terminology(
        (
            SdrfOntology(
                "ex",
                load_vocabulary(graph),
                OIO.id,
                synonym_predicates=(OIO.hasExactSynonym,),
                closed=closed,
            ),
        )
    )


def _schema() -> Any:
    """Build actual consumer schema objects with supported sentinel and parent rules."""
    return models.SchemaDefinition(
        name="local",
        description="Local terminology",
        columns=[
            models.ColumnDefinition(
                name=COLUMN,
                description="Cell type",
                requirement=models.RequirementLevel.REQUIRED,
                allow_not_applicable=True,
                validators=[
                    models.ValidatorConfig(
                        validator_name="ontology",
                        params={
                            "ontologies": ["ex"],
                            "parent_accession": "EX:Root",
                            "error_level": "error",
                        },
                    )
                ],
            )
        ],
    )


def _table(value: str) -> Any:
    """The actual parser sees reordered original physical columns."""
    return sdrf.read_sdrf(
        io.StringIO(
            "comment[batch]\tsource name\t" + COLUMN + "\nB\tsample\t" + value + "\n"
        )
    )


def test_original_validator_accepts_real_local_client_and_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No remote connection occurs and the maintained ontology class is unchanged."""

    def denied(*args: object, **kwargs: object) -> None:
        """Fail any actual attempted connection in this offline consumer test."""
        raise AssertionError("Unexpected network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    terminology = _terminology()
    assert terminology.report.verified
    client = local_sdrf_client(terminology)
    assert isinstance(client, ols.OlsClient)
    validator = validators.OntologyValidator(
        params={
            "ontologies": ["ex"],
            "parent_accession": "EX:Root",
            "error_level": "error",
        },
        client=client,
    )
    table = _table("NT=Child synonym;AC=EX:Child")
    assert validator.validate(table[COLUMN], COLUMN) == []
    original = table.df.copy(deep=True)
    result = check_sdrf(table, _schema(), terminology)
    assert result.report.verified and result.consumer_report.verified
    assert result.cells[2].value == "NT=Child synonym;AC=EX:Child"
    assert result.cells[2].column == 2 and table.df.equals(original)
    assert result.obligations[0].column == 2
    assert result.obligations[0].requirement == "required"
    assert result.obligations[0].parent_accession == "EX:Root"
    assert check_sdrf(_table("not applicable"), _schema(), terminology).report.verified
    rejected = _schema()
    rejected.columns[0].allow_not_applicable = False
    assert not check_sdrf(
        _table("not applicable"), rejected, terminology
    ).report.verified


def test_wrong_pair_and_parent_are_located_at_actual_original_cell() -> None:
    """Consumer errors are retained with original case and physical coordinates."""
    terminology = _terminology()
    for value in ("NT=Different;AC=EX:Child", "NT=Different;AC=EX:Other"):
        result = check_sdrf(_table(value), _schema(), terminology)
        assert result.report.outcome == "disagreement"
        assert any(
            issue.actual == value and issue.details.path == (0, 2)
            for issue in result.report
            if issue.details is not None
        )
        assert any(
            error.row == 0 and error.column == COLUMN for error in result.diagnostics
        )


def test_open_missing_accession_and_case_collisions_never_verify() -> None:
    """The consumer's lowercase and equal-parent shortcut cannot merge identifiers."""
    open_result = check_sdrf(
        _table("NT=Unknown;AC=EX:Unknown"), _schema(), _terminology(closed=False)
    )
    assert open_result.report.outcome == "unverified"
    assert open_result.diagnostics and not any(
        issue.kind == "finding" for issue in open_result.report
    )
    missing_source = _schema()
    missing_source.columns[0].validators[0].params["ontologies"] = ["unprovided"]
    unknown = check_sdrf(_table("NT=Child;AC=EX:Child"), missing_source, _terminology())
    assert unknown.report.outcome == "unverified"
    assert not any(issue.kind == "finding" for issue in unknown.report)
    colliding = _terminology(collision=True)
    result = check_sdrf(_table("NT=Child;AC=EX:Child"), _schema(), colliding)
    assert not result.report.verified and any(
        issue.code == "sdrf.unverified" for issue in result.report
    )
    client = local_sdrf_client(colliding)
    assert client.is_under_parent("EX:Child", "ex:child") is None
    assert check_sdrf(
        _table("NT=Child;AC=EX:Child"), _schema(), _terminology()
    ).report.verified


def test_actual_registry_combination_and_custom_coverage(tmp_path: Path) -> None:
    """Real schema composition keeps requirement and reordered cell locations."""
    base = _schema()
    (tmp_path / "base.yaml").write_text(
        "name: base\ndescription: base\nvalidators:\n"
        "  - validator_name: empty_cells\ncolumns:\n"
        "  - name: source name\n    description: source\n"
        "    requirement: required\n"
    )
    import yaml  # type: ignore[import-untyped]

    (tmp_path / "ontology.yaml").write_text(
        yaml.safe_dump(base.model_dump(mode="json"))
    )
    registry = registry_module.SchemaRegistry(
        schema_dir=str(tmp_path), use_versioned=False
    )
    combined = registry.combine_schemas(["base", "ontology"])
    assert (
        next(column for column in combined.columns if column.name == COLUMN).requirement
        == models.RequirementLevel.REQUIRED
    )
    terminology = _terminology()
    assert check_sdrf(
        _table("NT=Child;AC=EX:Child"), combined, terminology
    ).report.verified
    combined.columns[-1].validators.append(
        models.ValidatorConfig(
            validator_name="unavailable_custom", params={"authority": "external"}
        )
    )
    assert not check_sdrf(
        _table("NT=Child;AC=EX:Child"), combined, terminology
    ).report.verified
    inherited = _schema()
    inherited.extends = "unprovided-parent"
    assert (
        check_sdrf(
            _table("NT=Child;AC=EX:Child"), inherited, terminology
        ).report.outcome
        == "unverified"
    )
    no_ontology = models.SchemaDefinition(
        name="none", description="No ontology rules", columns=[]
    )
    assert (
        check_sdrf(_table("plain"), no_ontology, terminology).report.outcome
        == "unverified"
    )


def test_cumulative_bounds_and_required_prefixes() -> None:
    """Limited checks cannot certify partial cells or spend a fresh traversal budget."""
    terminology = _terminology()
    for maximum in (1, 50):
        result = check_sdrf(
            _table("NT=Child;AC=EX:Child"),
            _schema(),
            terminology,
            limits=Limits(max_work=maximum),
        )
        assert result.report.outcome == "exhausted"
    budget = Budget(Limits(max_work=100000))
    first = check_sdrf(
        _table("NT=Child;AC=EX:Child"), _schema(), terminology, budget=budget
    )
    assert first.report.verified and budget.remaining("work") < budget.limits.max_work

    graph = Graph()
    for iri, accession in ((E.A, "EX:Same"), (E.Z, "ex:same")):
        graph.add((iri, OIO.id, Literal(accession)))
        graph.add((iri, RDFS.label, Literal("Same")))
    source = SdrfOntology("ex", load_vocabulary(graph), OIO.id, closed=True)
    measured = Budget()
    complete = prepare_sdrf_terminology((source,), budget=measured)
    assert complete.resolve("EX:Same", Budget()) is None
    used = measured.limits.max_work - measured.remaining("work")
    for maximum in range(1, used + 1):
        partial = prepare_sdrf_terminology((source,), limits=Limits(max_work=maximum))
        if len(partial.terms) == 1 and partial.report.outcome == "exhausted":
            assert partial.resolve("EX:Same", Budget()) is None
            client = local_sdrf_client(partial)
            assert client.search("Same", ontology="ex") == []
            assert client.is_under_parent("EX:Same", "EX:Same") is None
            assert (
                check_sdrf(
                    _table("NT=Same;AC=EX:Same"), _schema(), partial
                ).report.outcome
                == "exhausted"
            )
            break
    else:
        raise AssertionError("No partial-index cutoff was exercised")


def test_unqualified_required_imports_cannot_establish_absence() -> None:
    """Raw imported vocabularies retain positives without claiming closed absence."""
    graph = Graph()
    for iri, accession, label in (
        (E.Root, "EX:Root", "Root"),
        (E.Child, "EX:Child", "Child"),
        (E.Other, "EX:Other", "Different"),
    ):
        graph.add((iri, RDF.type, OWL.Class))
        graph.add((iri, OIO.id, Literal(accession)))
        graph.add((iri, RDFS.label, Literal(label)))
    graph.add((E.Child, RDFS.subClassOf, E.Root))
    graph.add((E.Ontology, OWL.imports, E.Required))
    raw = prepare_sdrf_terminology(
        (SdrfOntology("ex", Vocabulary(graph), OIO.id, closed=True),)
    )
    assert raw.report.outcome == "unverified"
    client = local_sdrf_client(raw)
    assert client.is_under_parent("EX:Child", "EX:Root") is True
    assert client.is_under_parent("EX:Other", "EX:Root") is None
    for cell in ("NT=Different;AC=EX:Other", "NT=Missing;AC=EX:Missing"):
        result = check_sdrf(_table(cell), _schema(), raw)
        assert result.report.outcome == "unverified"
        assert not any(issue.kind == "finding" for issue in result.report)
    imported = Graph().add((E.Other, RDFS.subClassOf, E.Root))
    qualified = load_vocabulary(graph, imports={str(E.Required): imported})
    selected = prepare_sdrf_terminology(
        (SdrfOntology("ex", qualified, OIO.id, closed=True),)
    )
    assert selected.report.verified
    assert check_sdrf(
        _table("NT=Different;AC=EX:Other"), _schema(), selected
    ).report.verified

    lookup = load_vocabulary(Graph(), lookup=(graph,))
    assert lookup.required_imports_qualified
    lookup_source = inspect_vocabulary(lookup, scopes=("lookup",))
    selected_lookup = prepare_sdrf_terminology(
        (SdrfOntology("ex", lookup_source, OIO.id, closed=True),)
    )
    assert selected_lookup.report.outcome == "unverified"
    lookup_client = local_sdrf_client(selected_lookup)
    assert lookup_client.is_under_parent("EX:Child", "EX:Root") is True
    assert lookup_client.is_under_parent("EX:Other", "EX:Root") is None
    assert (
        check_sdrf(
            _table("NT=Missing;AC=EX:Missing"), _schema(), selected_lookup
        ).report.outcome
        == "unverified"
    )
