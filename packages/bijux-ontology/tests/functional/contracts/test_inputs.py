"""Owned decoding rejects ambiguity and bounds before copying caller data."""

import pytest
from rdflib import URIRef

from bijux_ontology.diagnostics.errors import InputError, LimitExceeded
from bijux_ontology.diagnostics.reports import Issue, Report
from bijux_ontology.execution.limits import Budget, IssueCollector, Limits
from bijux_ontology.json.values import decode_json, encode_json, own_json
from bijux_ontology.rdf.identifiers import absolute_iri, language_tag


def test_exact_identifiers_and_language_tags_use_rdf_validation() -> None:
    for value in (
        "urn:example:record",
        "https://example.org/Å",
        "http://example.org/x",
    ):
        assert absolute_iri(value) == value
    for value in ("relative", "_:blank", "https://bad host/x", "@id"):
        with pytest.raises(InputError):
            absolute_iri(value)
    for value in ("en-GB", "sv", "zh-Hant", "i-klingon"):
        assert language_tag(value) == value
    with pytest.raises(InputError):
        language_tag("not_a_tag")


@pytest.mark.parametrize(
    "payload, location",
    [(' {"a":1,"a":2}', "/a"), ('{"items":[{"x/y":1,"x/y":2}]}', "/items/0/x~1y")],
)
def test_duplicate_names_have_their_real_object_path(
    payload: str, location: str
) -> None:
    with pytest.raises(InputError) as error:
        decode_json(payload, source="contract.json")
    assert error.value.location == "contract.json" + location
    assert isinstance(error.value.__cause__, ValueError)


def test_parsed_json_is_owned_and_cannot_bypass_bounds() -> None:
    supplied = {"labels": ["Å"]}
    owned = own_json(supplied)
    supplied["labels"].append("Changed")
    assert owned == {"labels": ["Å"]}
    assert decode_json(b'{"ok":false,"empty":""}') == {"ok": False, "empty": ""}
    with pytest.raises(LimitExceeded):
        own_json({"label": "Å" * 5}, max_bytes=20)
    with pytest.raises(LimitExceeded):
        decode_json('{"label":"ÅÅÅÅÅ"}', max_bytes=20)
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(InputError, match="Cyclic"):
        own_json(cyclic)
    nested: object = "value"
    for _ in range(130):
        nested = [nested]
    with pytest.raises(LimitExceeded):
        own_json(nested)
    with pytest.raises(InputError):
        decode_json('{"value":NaN}')


def test_work_budgets_are_local_and_name_the_exhausted_bound() -> None:
    limits = Limits(max_states=1)
    first, second = Budget(limits), Budget(limits)
    first.consume("states")
    second.consume("states")
    with pytest.raises(LimitExceeded, match="max_states"):
        first.consume("states")
    with pytest.raises(InputError):
        Limits(max_work=-1)
    issues = IssueCollector(Budget(Limits(max_issues=1)))
    prior = Issue("example.missing", "Missing term", "record.name")
    issues.append(prior)
    with pytest.raises(LimitExceeded) as error:
        issues.append(Issue("example.other", "Another finding"))
    issues.exhausted(str(error.value))
    assert issues[0] == prior
    assert issues[1].kind == "exhausted"
    report = Report("bounded", 1, tuple(issues))
    assert not report.complete
    assert report.exit_code() == 2


def test_explicit_large_json_depth_cannot_leak_runtime_recursion() -> None:
    value: object = None
    for _ in range(1200):
        value = [value]
    with pytest.raises(LimitExceeded, match="runtime nesting"):
        own_json(value, max_depth=2000)


def test_raw_json_size_is_checked_before_allocating_its_utf8_copy() -> None:
    """A small admission limit cannot trigger a copy of the whole oversized input."""
    import tracemalloc

    payload = " " * 1_000_000 + "{}"
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        with pytest.raises(LimitExceeded) as failure:
            decode_json(payload, source="oversized.json", max_bytes=32)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
    assert failure.value.location == "oversized.json"
    assert peak - current < 100_000
    assert decode_json(" {} ", max_bytes=32) == {}


def test_input_context_preserves_the_original_native_parse_failure() -> None:
    """Configuration paths cannot erase the actual failing content and parser span."""
    from bijux_ontology.diagnostics.reports import Location
    from bijux_ontology.json.values import at_input_location
    from bijux_ontology.rdf.sources.loading import prepare_rdf
    from bijux_ontology.rdf.sources.types import RdfSource

    with pytest.raises(InputError) as original:
        prepare_rdf(
            RdfSource(
                b"broken RDF",
                format="turtle",
                document_iri="https://example.org/data.ttl",
            )
        )
    evidence = original.value.failure
    assert evidence is not None and evidence.span is not None
    with (
        pytest.raises(type(original.value)) as wrapped,
        at_input_location("model.json", ("models", 0)),
    ):
        raise original.value
    assert wrapped.value.location == "model.json#/models/0"
    assert wrapped.value.failure is evidence
    location = Location.from_error(wrapped.value)
    assert location is not None and location.failure is evidence
    assert location.source == "https://example.org/data.ttl"


def test_owned_json_names_are_unique_exact_wire_strings() -> None:
    """JSON names cannot inherit RDF key equality or produce duplicate output."""
    supplied = {URIRef("name"): [{URIRef("value"): URIRef("urn:value")}]}
    owned = own_json(supplied)
    assert isinstance(owned, dict) and type(next(iter(owned))) is str
    children = owned["name"]
    assert isinstance(children, list) and isinstance(children[0], dict)
    assert type(next(iter(children[0]))) is str
    assert decode_json(encode_json(owned, budget=Budget())) == {
        "name": [{"value": "urn:value"}]
    }
    supplied[URIRef("name")].append({})
    assert len(children) == 1
    shared = URIRef("shared")
    aliases = own_json({shared: shared})
    assert isinstance(aliases, dict)
    assert type(next(iter(aliases))) is str and type(aliases["shared"]) is str
    assert aliases == {"shared": "shared"}
    for aliases in ({URIRef("p"): 1, "p": 2}, {"p": 2, URIRef("p"): 1}):
        for value in (aliases, {"nested": [aliases]}):
            with pytest.raises(InputError, match="collide"):
                own_json(value, source="names.json")
            with pytest.raises(InputError, match="collide"):
                encode_json(value, budget=Budget())
    stopped = Budget(Limits(max_states=1))
    with pytest.raises(LimitExceeded):
        own_json({URIRef("p"): "value"}, budget=stopped)


def test_string_subclass_value_storage_is_paid_before_copying() -> None:
    """Deepcopy of a string subclass cannot allocate outside its shared grant."""
    import tracemalloc

    value = URIRef("p" * 250_000)
    limits = Limits(max_bytes=1_000_000, max_total_bytes=1024, max_work=1_000_000)
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    current, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        with pytest.raises(LimitExceeded):
            own_json({"value": value}, budget=Budget(limits))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
    assert peak - current < 100_000
    ordinary = str.__str__(value)
    assert own_json({"value": ordinary}, budget=Budget(limits)) == {"value": ordinary}
