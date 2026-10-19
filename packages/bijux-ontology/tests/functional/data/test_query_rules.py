"""Consumer-owned competency files retain asserted answers and exact failing terms."""

import json
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import overload

import pytest
from rdflib import BNode, Graph, Literal, Namespace
from rdflib.namespace import OWL, RDF, RDFS, XSD

from bijux_ontology.data.query_rules import (
    QueryAnswers,
    QueryRule,
    load_query_rules,
    query_rules_from_data,
    run_query_rules,
)
from bijux_ontology.diagnostics.errors import InputError, ResourceError
from bijux_ontology.documents.preparation import prepare_source
from bijux_ontology.execution.limits import Limits
from bijux_ontology.rdf.queries import QueryScope
from bijux_ontology.rdf.sources.types import RdfSource

EX = Namespace("https://rules.example/")
DEFAULT = QueryScope("default")


def source() -> Graph:
    return (
        Graph()
        .add((EX.Parent, RDF.type, OWL.Class))
        .add((EX.Child, RDF.type, OWL.Class))
        .add((EX.Child, RDFS.subClassOf, EX.Parent))
        .add((EX.Parent, RDFS.label, Literal("Parent")))
    )


def test_ordinary_query_files_are_owned_and_identify_the_exact_failing_term(
    tmp_path: Path,
) -> None:
    (tmp_path / "ontology.ttl").write_text(source().serialize(format="turtle"))
    (tmp_path / "hierarchy.rq").write_text(
        f"ASK {{ <{EX.Child}> <{RDFS.subClassOf}>+ <{EX.Parent}> }}"
    )
    (tmp_path / "labels.rq").write_text(
        f"SELECT ?term WHERE {{?term a <{OWL.Class}> "
        f"FILTER NOT EXISTS {{?term <{RDFS.label}> ?label}}}}"
    )
    specification = [
        {
            "name": "hierarchy",
            "query_file": "hierarchy.rq",
            "scope": {"kind": "default"},
            "expected": True,
        },
        {"name": "labels", "query_file": "labels.rq", "scope": {"kind": "default"}},
    ]
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(specification))
    rules = load_query_rules(path)
    (tmp_path / "hierarchy.rq").write_text("ASK { invalid")
    result = run_query_rules(prepare_source(tmp_path / "ontology.ttl"), rules)
    assert result.report.outcome == "disagreement" and result.report.complete
    assert [name for name, _ in result.results] == ["hierarchy", "labels"]
    assert result.results[0][1].answer is True
    assert result.results[1][1].rows == ((EX.Child,),)
    finding = next(i for i in result.report.issues if i.code == "data.query_violation")
    assert finding.details is not None and finding.details.term == str(EX.Child)
    assert finding.details.source == str(tmp_path / "labels.rq")
    assert finding.evidence == {
        "rule": "labels",
        "bindings": {"term": {"kind": "iri", "value": str(EX.Child)}},
    }


def test_wrong_expectation_query_error_and_truncation_remain_distinct() -> None:
    prepared = prepare_source(source())
    wrong = run_query_rules(prepared, (QueryRule("wrong", "ASK {}", DEFAULT, False),))
    assert wrong.report.outcome == "disagreement" and wrong.results[0][1].answer is True
    zero = run_query_rules(
        prepared,
        (QueryRule("zero", f"SELECT ?s WHERE {{?s <{EX.absent}> ?v}}", DEFAULT),),
    )
    assert zero.report.verified and zero.results[0][1].rows == ()
    kind = run_query_rules(prepared, (QueryRule("kind", "ASK {}", DEFAULT),))
    assert kind.report.outcome == "input_error"
    error = run_query_rules(
        prepared, (QueryRule("syntax", "SELECT * WHERE {", DEFAULT),)
    )
    assert error.report.outcome == "input_error" and error.results[0][1].kind is None
    assert not any(i.kind == "finding" for i in error.report.issues)
    rules = (
        QueryRule(
            "types",
            f"SELECT ?term WHERE {{?term a <{OWL.Class}>}} ORDER BY ?term",
            DEFAULT,
        ),
        QueryRule("pending", "ASK {}", DEFAULT, True),
    )
    truncated = run_query_rules(prepared, rules, max_rows=1)
    assert truncated.report.outcome == "exhausted" and not truncated.report.complete
    assert len(truncated.results) == 1 and len(truncated.results[0][1].rows) == 1
    assert any(i.code == "data.query_violation" for i in truncated.report.issues)
    assert any("not started" in i.message for i in truncated.report.issues)


def test_expected_answer_sets_compare_exact_lexical_terms_unbound_and_blank_scope() -> (
    None
):
    graph = Graph().add(
        (EX.value, EX.number, Literal("01", datatype=XSD.integer, normalize=False))
    )
    prepared = prepare_source(graph)
    rule = QueryRule(
        "number",
        f"SELECT ?number WHERE {{?s <{EX.number}> ?number}}",
        DEFAULT,
        QueryAnswers(
            ("number",), ((Literal("01", datatype=XSD.integer, normalize=False),),)
        ),
    )
    assert run_query_rules(prepared, (rule,)).report.verified
    wrong = replace(
        rule,
        expected=QueryAnswers(
            ("number",), ((Literal("1", datatype=XSD.integer, normalize=False),),)
        ),
    )
    mismatch = run_query_rules(prepared, (wrong,))
    assert {i.code for i in mismatch.report.issues} == {
        "data.query_answer_missing",
        "data.query_answer_unexpected",
    }
    assert mismatch.results[0][1].rows[0][0] == Literal(
        "01", datatype=XSD.integer, normalize=False
    )
    unbound = QueryRule(
        "unbound",
        "SELECT (?missing AS ?v) WHERE {}",
        DEFAULT,
        QueryAnswers(("v",), ((None,),)),
    )
    assert run_query_rules(prepared, (unbound,)).report.verified
    with pytest.raises(InputError, match="blank"):
        QueryAnswers(("node",), ((BNode("label"),),))
    blank = run_query_rules(
        prepare_source(Graph().add((BNode(), EX.p, EX.value))),
        (QueryRule("blank", "SELECT ?node WHERE {?node ?p ?o}", DEFAULT),),
    )
    evidence = next(
        i.evidence for i in blank.report.issues if i.code == "data.query_violation"
    )
    assert evidence is not None
    bindings = evidence["bindings"]
    assert isinstance(bindings, dict)
    node = bindings["node"]
    assert isinstance(node, dict) and node["kind"] == "blank"


def test_required_nonempty_suite_and_scope_cannot_be_satisfied_vacuously() -> None:
    rule = QueryRule("question", "ASK {}", DEFAULT, True)
    empty = run_query_rules(prepare_source(Graph()), (rule,))
    assert empty.report.outcome == "input_error" and not empty.results
    assert any(i.code == "data.query_empty_scope" for i in empty.report.issues)
    explicit = run_query_rules(prepare_source(Graph()), (rule,), allow_empty=True)
    assert explicit.report.verified and explicit.report.exclusions == (
        "empty-data-selection",
    )
    assert not run_query_rules(prepare_source(source()), ()).report.verified
    from rdflib import Dataset

    dataset = Dataset()
    dataset.graph(EX.named).add((EX["term"], EX.p, EX.value))
    prepared = prepare_source(dataset)
    named = QueryRule(
        "named", f"ASK {{GRAPH <{EX.named}> {{?s ?p ?o}}}}", QueryScope("dataset"), True
    )
    assert run_query_rules(prepared, (named,)).report.verified
    pinned = prepare_source(RdfSource(dataset, graph_name=str(EX.named)))
    assert run_query_rules(
        pinned, (replace(rule, scope=QueryScope("named", EX.named)),)
    ).report.verified
    conflict = run_query_rules(pinned, (rule,))
    assert conflict.report.outcome == "input_error"


def test_loader_rejects_ignored_options_missing_files_and_undefined_expected_terms(
    tmp_path: Path,
) -> None:
    base = {
        "name": "rule",
        "query": "ASK {}",
        "scope": {"kind": "default"},
        "expected": True,
    }
    with pytest.raises(InputError, match="Unknown"):
        query_rules_from_data([{**base, "ignored": True}], directory=tmp_path)
    with pytest.raises(ResourceError):
        query_rules_from_data(
            [
                {k: v for k, v in base.items() if k != "query"}
                | {"query_file": "missing.rq"}
            ],
            directory=tmp_path,
        )
    with pytest.raises(InputError, match="unique"):
        query_rules_from_data([base, base], directory=tmp_path)
    blank = {
        **base,
        "query": "SELECT ?v WHERE {}",
        "expected": {
            "variables": ["v"],
            "rows": [[{"kind": "blank", "value": "caller"}]],
        },
    }
    with pytest.raises(InputError, match="blank"):
        query_rules_from_data([blank], directory=tmp_path)


def test_suite_bounds_retain_completed_findings_and_required_pending_inventory() -> (
    None
):
    prepared = prepare_source(source())
    rules = tuple(
        QueryRule(f"rule{index}", "ASK {}", DEFAULT, True) for index in range(4)
    )
    deadline = run_query_rules(prepared, rules, timeout=0.000001)
    assert deadline.report.outcome == "exhausted" and not deadline.report.complete
    bounded = run_query_rules(prepared, rules, limits=Limits(max_resources=2))
    assert len(bounded.results) == 2 and all(
        result.report.verified for _, result in bounded.results
    )
    assert bounded.report.outcome == "exhausted" and not bounded.report.complete
    assert any("2 required" in issue.message for issue in bounded.report.issues)
    violations = QueryRule("many", "SELECT ?s ?p ?o WHERE {?s ?p ?o}", DEFAULT)
    limited = run_query_rules(
        prepared, (violations, rules[0]), limits=Limits(max_issues=1)
    )
    assert limited.report.outcome == "exhausted" and not limited.report.complete
    assert (
        sum(issue.code == "data.query_violation" for issue in limited.report.issues)
        == 1
    )
    assert len(limited.results) == 1
    capture_limit = run_query_rules(prepared, rules, limits=Limits(max_work=1))
    assert capture_limit.report.outcome == "exhausted" and not capture_limit.results
    assert capture_limit.report.requested == len(rules)


def test_late_inventory_capture_exhausts_before_another_item_or_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_source(source())
    calls = []
    rule = QueryRule("slow", "ASK {}", DEFAULT, True)

    class SlowRules(Sequence[QueryRule]):
        """Expose an actual slow caller getter without any query-side delay."""

        def __len__(self) -> int:
            return 100

        @overload
        def __getitem__(self, index: int) -> QueryRule: ...

        @overload
        def __getitem__(self, index: slice) -> Sequence[QueryRule]: ...

        def __getitem__(self, index: int | slice) -> QueryRule | Sequence[QueryRule]:
            if isinstance(index, slice):
                return ()
            calls.append(index)
            time.sleep(0.02)
            return rule

    def unexpected_query(*args: object, **kwargs: object) -> None:
        raise AssertionError("Late declaration capture must never start a worker")

    monkeypatch.setattr(
        "bijux_ontology.data.query_rules.checking._query_rdf", unexpected_query
    )
    result = run_query_rules(prepared, SlowRules(), timeout=0.005)
    assert result.report.outcome == "exhausted" and not result.report.complete
    assert not result.results and calls == [0]
    assert result.report.requested == 100
    assert (
        "100 required query rules were not started" in result.report.issues[0].message
    )


def test_expired_suite_does_not_restart_a_child_deadline_after_host_delay() -> None:
    """A real host scheduling delay cannot admit native work on an expired suite."""
    import subprocess
    import sys
    import time
    from types import FrameType
    from typing import Literal as EventKind

    from bijux_ontology.rdf.queries import access

    source = prepare_source(RdfSource("<urn:s> <urn:p> <urn:o>.", format="turtle"))
    delayed: list[bool] = []
    children: list[object] = []
    previous = sys.getprofile()

    def observe(
        frame: FrameType,
        event: EventKind["call", "return", "c_call", "c_return", "c_exception"],
        returned: object,
    ) -> None:
        if previous is not None:
            previous(frame, event, returned)
        if (
            event == "call"
            and frame.f_code is access._query_rdf.__code__
            and not delayed
        ):
            delayed.append(True)
            time.sleep(0.15)
        if event == "call" and frame.f_code is subprocess.Popen.__init__.__code__:
            children.append(frame.f_locals.get("args"))

    sys.setprofile(observe)
    try:
        result = run_query_rules(
            source,
            (QueryRule("answer", "ASK {}", QueryScope("default"), True),),
            timeout=0.05,
        )
    finally:
        sys.setprofile(previous)
    assert delayed == [True] and children == []
    assert result.report.outcome == "exhausted" and not result.report.complete
    assert result.results == ()
    assert run_query_rules(
        source, (QueryRule("answer", "ASK {}", QueryScope("default"), True),)
    ).report.verified
