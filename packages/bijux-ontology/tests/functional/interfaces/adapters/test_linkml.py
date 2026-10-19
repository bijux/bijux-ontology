"""Real induced slots, explicit output knowledge and controlled import boundaries."""

import pytest
from linkml_runtime.utils.schemaview import SchemaView  # type: ignore[import-untyped]
from rdflib.namespace import XSD

from bijux_ontology.declarations import ValueDeclaration as Value
from bijux_ontology.diagnostics.errors import ResourceError
from bijux_ontology.interfaces.adapters.contracts import RdfField
from bijux_ontology.interfaces.adapters.linkml import model_declarations

SCHEMA = """
id: https://example.org/catalogue
name: catalogue
prefixes:
  ex: https://example.org/
classes:
  Record:
    class_uri: ex:Record
    slots: [title]
  SpecialRecord:
    is_a: Record
    class_uri: ex:SpecialRecord
slots:
  title:
    slot_uri: ex:title
    range: string
    required: true
types:
  string:
    uri: xsd:string
"""


def test_effective_inherited_slots_use_explicit_serializer_metadata() -> None:
    view = SchemaView(SCHEMA)
    contract = RdfField(
        "https://example.org/title",
        (Value("literal", datatype=str(XSD.string)),),
        output_contract=True,
    )
    before = view.schema.classes["SpecialRecord"].slots.copy()
    result = model_declarations(
        view,
        classes=("SpecialRecord",),
        field_contracts={"SpecialRecord": {"title": contract}},
    )
    assert result.report.verified
    model = result.declarations[0]
    assert model.types == ("https://example.org/SpecialRecord",)
    assert (
        model.fields[0].name == "title"
        and model.fields[0].predicate == "https://example.org/title"
    )
    assert model.fields[0].emission == "unknown"
    assert view.schema.classes["SpecialRecord"].slots == before
    unknown = model_declarations(view, classes=("SpecialRecord",))
    assert unknown.declarations[0].fields[0].values[0].kind == "unknown"
    assert not unknown.report.verified


def test_unprepared_imports_do_not_trigger_implicit_framework_retrieval() -> None:
    view = SchemaView(SCHEMA + "\nimports:\n  - https://example.org/never-fetch\n")
    with pytest.raises(ResourceError, match="does not retrieve imports"):
        model_declarations(view)


def test_induced_slot_limits_preserve_completed_models_and_diagnostics() -> None:
    from bijux_ontology.limits import Limits

    result = model_declarations(SchemaView(SCHEMA), limits=Limits(max_issues=1))
    assert len(result.declarations) == 1
    assert result.report.exit_code() == 2 and not result.report.complete
    assert [issue.code for issue in result.report.issues] == [
        "adapter.output_unverified",
        "execution.limit_exceeded",
    ]
    bounded = model_declarations(SchemaView(SCHEMA), limits=Limits(max_work=1))
    assert not bounded.declarations and bounded.report.exit_code() == 2
