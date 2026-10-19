"""Actual maintained rule passes retain exact RDF and checked acceptance scope."""

import subprocess
import threading
from collections.abc import Sequence
from typing import Any, TypedDict

import pytest
from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, SH, XSD
from rdflib.term import Node

from bijux_ontology.diagnostics.errors import LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.documents.migrations import export_migration
from bijux_ontology.limits import Budget, IssueCollector, Limits
from bijux_ontology.rdf.loading import prepare_rdf
from bijux_ontology.rdf.migrations.candidates import MigrationCheck
from bijux_ontology.rdf.snapshots import PreparedSource
from bijux_ontology.rdf.sources import RdfSource
from bijux_ontology.shacl.checking import validate_shapes
from bijux_ontology.shacl.functions import (
    ShaclFunctionCatalog,
    ShaclFunctionParameter,
    ShaclSparqlFunction,
)
from bijux_ontology.shacl.rules import RuleMode, materialize_rules

EX = Namespace("urn:rules:")
PROGRAM = """@prefix e:<urn:rules:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
e:Consumer a sh:NodeShape; sh:order 0; sh:targetNode e:r;
 sh:rule [a sh:SPARQLRule; sh:construct
 "CONSTRUCT {?this <urn:rules:done> true} WHERE {?this <urn:rules:mid> true}"].
e:Producer a sh:NodeShape; sh:order 1; sh:targetNode e:r;
 sh:rule [a sh:SPARQLRule; sh:construct
 "CONSTRUCT {?this <urn:rules:mid> true} WHERE {?this <urn:rules:seed> true}"]."""


class _RuleArguments(TypedDict, total=False):
    """Reuse only concrete typed public keyword values across actual operations."""

    use_shapes: tuple[URIRef, ...]
    trusted: bool
    checks: tuple[MigrationCheck, ...]
    mode: RuleMode


def _data() -> PreparedSource:
    """Retain a noncanonical integer spelling alongside the triggering assertion."""
    return prepare_rdf(
        Graph()
        .add((EX.r, EX.seed, Literal(True)))
        .add((EX.r, EX.code, Literal("01", datatype=XSD.integer, normalize=False)))
    )


def _check(predicate: str = "done") -> MigrationCheck:
    """Use an independently authored actual SHACL output contract."""
    shapes = prepare_rdf(
        f"""@prefix e:<urn:rules:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
        e:Output a sh:NodeShape; sh:targetNode e:r; sh:property [
        sh:path e:{predicate}; sh:minCount 1; sh:datatype
        <http://www.w3.org/2001/XMLSchema#boolean>].""",
        format="turtle",
    )
    return MigrationCheck(
        "actual-output", lambda source: validate_shapes(source, shapes).report
    )


def test_ordered_pass_and_global_program_closure() -> None:
    """Full-program passes propagate facts and actual output checks govern export."""
    pytest.importorskip("pyshacl")
    data, rules = _data(), prepare_rdf(PROGRAM, format="turtle")
    original = set(data.graph())
    owners = (EX.Consumer, EX.Producer)
    ordered = materialize_rules(
        data, rules, checks=(_check("mid"),), use_shapes=owners, trusted=True
    )
    assert ordered.report.verified and ordered.passes == 1
    assert ordered.asserted is not None and set(ordered.asserted.graph()) == original
    assert ordered.derived is not None
    assert (EX.r, EX.mid, Literal(True)) in ordered.derived.graph()
    assert (EX.r, EX.done, None) not in ordered.derived.graph()
    closed = materialize_rules(
        data,
        rules,
        mode="global-fixed-point",
        checks=(_check(),),
        use_shapes=owners,
        trusted=True,
    )
    assert closed.report.verified and closed.passes == 3
    assert closed.derived is not None and len(closed.derived.graph()) == 2
    assert closed.candidate is not None and closed.candidate.source is not None
    assert closed.candidate.patch is not None
    assert not closed.candidate.patch.removals
    assert (
        EX.r,
        EX.code,
        Literal("01", datatype=XSD.integer, normalize=False),
    ) in closed.candidate.source.graph()
    assert set(data.graph()) == original
    assert export_migration(closed.candidate, format="turtle").content is None
    assert export_migration(
        closed.candidate, accept=True, format="turtle"
    ).report.verified
    rejected = materialize_rules(
        data, rules, checks=(_check(),), use_shapes=owners, trusted=True
    )
    assert rejected.report.outcome == "disagreement" and rejected.candidate is not None
    assert (
        export_migration(rejected.candidate, accept=True, format="turtle").content
        is None
    )


def test_triple_conditions_and_explicit_construct_function() -> None:
    """Maintained Core conditions and a separately trusted function execute normally."""
    pytest.importorskip("pyshacl")
    rules = prepare_rdf(
        """@prefix e:<urn:rules:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
        e:Root a sh:NodeShape;sh:targetNode e:r;sh:rule [a sh:TripleRule;
        sh:subject sh:this;sh:predicate e:done;sh:object true;
        sh:condition [sh:property [sh:path e:seed;sh:hasValue true]]].""",
        format="turtle",
    )
    result = materialize_rules(
        _data(), rules, use_shapes=(EX.Root,), trusted=True, checks=(_check(),)
    )
    assert result.report.verified
    false_condition = materialize_rules(
        prepare_rdf(Graph().add((EX.r, RDF.type, EX.Record))),
        rules,
        use_shapes=(EX.Root,),
        trusted=True,
        checks=(_check(),),
    )
    assert false_condition.report.outcome == "disagreement"
    function = ShaclSparqlFunction(
        str(EX.next),
        (
            ShaclFunctionParameter(
                "https://example.invalid/n", datatype=str(XSD.integer)
            ),
        ),
        select="SELECT (?n + 1 AS ?result) WHERE {}",
        return_type=str(XSD.integer),
    )
    catalog = ShaclFunctionCatalog.from_functions((function,), trusted=True)
    # Namespace-only prefixes are resolved through the actual supplied RDF graph.
    program = Graph()
    program.bind("e", EX)
    rule = BNode()
    for triple in (
        (EX.Root, RDF.type, SH.NodeShape),
        (EX.Root, SH.targetNode, EX.r),
        (EX.Root, SH.rule, rule),
        (rule, RDF.type, SH.SPARQLRule),
        (
            rule,
            SH.construct,
            Literal(
                "CONSTRUCT {?this e:amount ?v} "
                "WHERE {?this e:code ?n. BIND(e:next(?n) AS ?v)}"
            ),
        ),
    ):
        program.add(triple)
    observed: list[Node] = []

    def actual(source: PreparedSource) -> Report:
        """Check the actual maintained function result without changing the snapshot."""
        observed.extend(source.graph().objects(EX.r, EX.amount))
        return Report(
            "actual",
            1,
            ()
            if observed == [Literal(2)]
            else (Issue("wrong", "Wrong actual result"),),
        )

    result = materialize_rules(
        _data(),
        prepare_rdf(program),
        use_shapes=(EX.Root,),
        trusted=True,
        functions=catalog,
        checks=(MigrationCheck("function-output", actual),),
    )
    assert result.report.verified and observed == [Literal(2)]
    unavailable = materialize_rules(
        _data(),
        prepare_rdf(program),
        use_shapes=(EX.Root,),
        trusted=True,
        checks=(_check(),),
    )
    assert not unavailable.report.verified and unavailable.candidate is None


def test_fresh_blank_growth_exhausts_without_partial_candidate() -> None:
    """Fresh terms are compared in one worker scope rather than modulo isomorphism."""
    pytest.importorskip("pyshacl")
    growing = prepare_rdf(
        '''@prefix e:<urn:rules:>. @prefix sh:<http://www.w3.org/ns/shacl#>.
        e:Root a sh:NodeShape;sh:targetNode e:r;sh:rule [a sh:SPARQLRule;
        sh:construct """CONSTRUCT {?this <urn:rules:child>
        [<urn:rules:mark> true]} WHERE {}"""].''',
        format="turtle",
    )
    source = _data()
    original = set(source.graph())
    result = materialize_rules(
        source,
        growing,
        use_shapes=(EX.Root,),
        trusted=True,
        mode="global-fixed-point",
        max_passes=3,
        checks=(_check(),),
    )
    assert result.report.outcome == "exhausted"
    assert result.derived is None and result.candidate is None
    assert set(source.graph()) == original
    one = materialize_rules(
        source,
        growing,
        use_shapes=(EX.Root,),
        trusted=True,
        checks=(
            MigrationCheck(
                "actual-derived", lambda s: Report("actual", len(s.graph()))
            ),
        ),
    )
    assert one.report.verified and one.derived is not None
    assert len(one.derived.graph()) == 2
    anonymous = prepare_rdf(Graph().add((BNode(), EX.seed, Literal(True))))
    unknown = materialize_rules(
        anonymous, growing, use_shapes=(EX.Root,), trusted=True, checks=(_check(),)
    )
    assert unknown.report.outcome == "unverified" and unknown.candidate is None


def test_scope_and_unsupported_capabilities_are_required() -> None:
    """Pinned data stays selected; missing or ambient semantics cannot go green."""
    pytest.importorskip("pyshacl")
    dataset = Dataset()
    dataset.graph(EX.G).add((EX.r, EX.seed, Literal(True)))
    dataset.graph(EX.H).add((EX.secret, EX.hidden, Literal("unselected")))
    data = prepare_rdf(RdfSource(dataset, graph_name=str(EX.G)))
    rules = prepare_rdf(PROGRAM, format="turtle")
    arguments: _RuleArguments = {
        "use_shapes": (EX.Consumer, EX.Producer),
        "trusted": True,
        "checks": (_check(),),
        "mode": "global-fixed-point",
    }
    result = materialize_rules(data, rules, **arguments)
    assert result.report.verified and result.candidate is not None
    assert result.candidate.source is not None
    assert result.candidate.source.graph_name == str(EX.G)
    assert all(q[1] != EX.hidden for q in result.candidate.source.graph())
    assert not materialize_rules(
        prepare_rdf(dataset), rules, **arguments
    ).report.verified
    assert not materialize_rules(
        data, rules, use_shapes=(EX.Consumer, EX.Producer)
    ).report.verified
    assert not materialize_rules(
        data, rules, use_shapes=(EX.Consumer, EX.Producer), trusted=True
    ).report.verified
    for body in (
        PROGRAM.replace("sh:order 1", "sh:order 0"),
        PROGRAM
        + "\n<urn:rules:Consumer> sh:target [a sh:SPARQLTarget;"
        + 'sh:select "SELECT ?this WHERE {}"].',
        PROGRAM.replace(
            "CONSTRUCT {?this <urn:rules:mid> true} "
            "WHERE {?this <urn:rules:seed> true}",
            "CONSTRUCT {?this <urn:rules:mid> true} "
            "WHERE {SERVICE <http://invalid.local/> {?this ?p ?o}}",
        ),
        PROGRAM.replace("sh:order 0;", "sh:order 0;sh:deactivated true;"),
    ):
        checked = materialize_rules(
            _data(), prepare_rdf(body, format="turtle"), **arguments
        )
        assert not checked.report.verified and checked.candidate is None
    for text in (
        Literal("CONSTRUCT {?this <urn:rules:mid> true} WHERE {}", lang="en"),
        Literal("CONSTRUCT {?this <urn:rules:mid> true} WHERE {}", datatype=XSD.anyURI),
    ):
        unsupported = rules.graph()
        for rule in unsupported.subjects(SH.construct):
            unsupported.set((rule, SH.construct, text))
        checked = materialize_rules(_data(), prepare_rdf(unsupported), **arguments)
        assert not checked.report.verified and checked.candidate is None


def test_actual_deadline_cancel_limits_and_recovery() -> None:
    """Owned deadlines, cancellation and aggregate quotas block partial output."""
    pytest.importorskip("pyshacl")
    rules, data = prepare_rdf(PROGRAM, format="turtle"), _data()
    arguments: _RuleArguments = {
        "use_shapes": (EX.Consumer, EX.Producer),
        "trusted": True,
        "checks": (_check(),),
        "mode": "global-fixed-point",
    }
    deadline = materialize_rules(data, rules, timeout=0.02, **arguments)
    assert deadline.report.outcome == "exhausted" and deadline.candidate is None
    cancel = threading.Event()
    cancel.set()
    cancelled = materialize_rules(data, rules, cancel=cancel, **arguments)
    assert cancelled.report.outcome == "exhausted" and cancelled.candidate is None
    for limits in (
        Limits(max_work=10),
        Limits(max_states=10),
        Limits(max_bytes=100),
        Limits(max_total_bytes=100),
        Limits(max_triples=10),
    ):
        bounded = materialize_rules(data, rules, limits=limits, **arguments)
        assert bounded.report.outcome == "exhausted" and bounded.candidate is None
    spent = Budget(Limits(max_issues=1))
    IssueCollector(spent).append(Issue("already.paid", "Existing ordinary issue"))
    unknown = materialize_rules(data, rules, use_shapes=(EX.Consumer,), budget=spent)
    assert unknown.report.outcome == "exhausted" and unknown.candidate is None
    assert materialize_rules(data, rules, **arguments).report.verified


def test_late_check_exhaustion_retains_known_failure() -> None:
    """A known callback failure survives a later quota failure."""
    pytest.importorskip("pyshacl")
    budget = Budget()

    def failing(source: PreparedSource) -> Report:
        """Establish a real retained contradiction from the candidate graph."""
        assert (EX.r, EX.mid, Literal(True)) in source.graph()
        return Report(
            "application",
            1,
            (
                Issue(
                    "application.rejected",
                    "Candidate violates the application contract",
                ),
            ),
        )

    def exhausted(source: PreparedSource) -> Report:
        """Spend the same actual operation allowance after the first checked fact."""
        budget.consume("work", budget.remaining("work") + 1)
        raise LimitExceeded("Unreachable")

    result = materialize_rules(
        _data(),
        prepare_rdf(PROGRAM, format="turtle"),
        use_shapes=(EX.Consumer, EX.Producer),
        trusted=True,
        checks=(
            MigrationCheck("rejected", failing),
            MigrationCheck("later", exhausted),
        ),
        budget=budget,
    )
    assert result.report.outcome == "exhausted" and not result.report.verified
    assert any(issue.code == "application.rejected" for issue in result.report.issues)
    assert result.candidate is not None
    assert (
        export_migration(result.candidate, accept=True, format="turtle").content is None
    )


def test_killed_rule_worker_is_unverified_and_original_inputs_recover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual native execution failure must not accuse valid supplied RDF."""
    pytest.importorskip("pyshacl")
    data, rules = _data(), prepare_rdf(PROGRAM, format="turtle")
    original_data, original_rules = set(data.graph()), set(rules.graph())
    real_popen = subprocess.Popen
    owned: list[subprocess.Popen[bytes]] = []

    def launch(command: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Kill only the actual isolated rule executor, preserving its native input."""
        process = real_popen(command, **kwargs)
        if "bijux_ontology.shacl.rules.worker" in command:
            owned.append(process)
            process.kill()
        return process

    with monkeypatch.context() as fault:
        fault.setattr(subprocess, "Popen", launch)
        failed = materialize_rules(
            data,
            rules,
            use_shapes=(EX.Consumer, EX.Producer),
            trusted=True,
            checks=(_check("mid"),),
        )
    assert owned and all(process.poll() is not None for process in owned)
    assert failed.report.outcome == "unverified" and not failed.report.complete
    assert failed.candidate is None and failed.derived is None
    assert any(issue.code == "shacl.rules_unverified" for issue in failed.report)
    assert not any(issue.kind == "input_error" for issue in failed.report)
    assert set(data.graph()) == original_data and set(rules.graph()) == original_rules
    recovered = materialize_rules(
        data,
        rules,
        use_shapes=(EX.Consumer, EX.Producer),
        trusted=True,
        checks=(_check("mid"),),
    )
    assert recovered.report.verified and recovered.candidate is not None
    invalid = materialize_rules(
        data,
        rules,
        use_shapes=(EX.Consumer, EX.Producer),
        trusted=True,
        max_passes=0,
        checks=(_check("mid"),),
    )
    assert invalid.report.outcome == "input_error" and invalid.candidate is None
