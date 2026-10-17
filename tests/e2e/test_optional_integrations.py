"""Installed optional integrations execute real backends and public contracts."""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.parametrize(
    "program",
    [
        """
        from rdflib import BNode, Graph, Literal, Namespace
        from rdflib.namespace import RDF, SH
        from bijux_ontology.shacl import validate_shapes
        ex = Namespace("https://example.org/")
        data = Graph().add((ex.record, ex.name, Literal("Image")))
        shapes = Graph().add((ex.Shape, RDF.type, SH.NodeShape))
        shapes.add((ex.Shape, SH.targetNode, ex.record))
        property_shape = BNode()
        shapes.add((ex.Shape, SH.property, property_shape))
        shapes.add((property_shape, SH.path, ex.name))
        shapes.add((property_shape, SH.minCount, Literal(1)))
        assert validate_shapes(data, shapes).conforms is True
        data.remove((ex.record, ex.name, None))
        result = validate_shapes(data, shapes)
        assert result.conforms is False and result.report.exit_code() == 1
        assert result.findings[0].constraint_component == SH.MinCountConstraintComponent
        assert len(result.report_graph()) > 0
        """,
        """
        from typing import Annotated
        from pydantic import BaseModel, Field
        from rdflib.namespace import XSD
        from bijux_ontology.interfaces.adapters.contracts import RdfField
        from bijux_ontology.interfaces.adapters.pydantic import model_declarations
        from bijux_ontology.declarations import ValueDeclaration
        from bijux_ontology.models import ModelUsage, check_emission
        class Record(BaseModel):
            title: Annotated[str, RdfField(
                "https://example.org/name",
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True, emission="guaranteed",
            )] = Field(serialization_alias="name")
        extraction = model_declarations({Record: ("https://example.org/Record",)})
        assert extraction.report.verified
        declaration = extraction.declarations[0]
        assert declaration.fields[0].key == "name"
        document = {"@context": {"@vocab": "https://example.org/"},
                    "@type": "Record",
                    **Record(title="Image").model_dump(by_alias=True)}
        assert check_emission(document, extraction.declarations,
                              [ModelUsage(declaration.identifier)]).verified
        document["name"] = 5
        result = check_emission(document, extraction.declarations,
                                [ModelUsage(declaration.identifier)])
        assert result.exit_code() == 1
        assert result.issues[0].code == "emission.value_mismatch"
        """,
        """
        from linkml_runtime.utils.schemaview import SchemaView
        from linkml_runtime.linkml_model.meta import SchemaDefinition
        from rdflib.namespace import XSD
        from bijux_ontology.interfaces.adapters.contracts import RdfField
        from bijux_ontology.interfaces.adapters.linkml import model_declarations
        from bijux_ontology.declarations import ValueDeclaration
        view = SchemaView(SchemaDefinition(**{
            "id": "https://example.org/schema", "name": "catalogue",
            "prefixes": {"ex": "https://example.org/"},
            "classes": {
                "Record": {"class_uri": "ex:Record", "slots": ["name"]},
                "Image": {"is_a": "Record", "class_uri": "ex:Image"},
            },
            "slots": {"name": {"slot_uri": "ex:name", "range": "string"}},
            "types": {"string": {"uri": "xsd:string"}},
        }))
        contract = RdfField("https://example.org/name",
            (ValueDeclaration("literal", str(XSD.string)),), output_contract=True)
        result = model_declarations(view, classes=("Image",),
                                   field_contracts={"Image": {"name": contract}})
        assert result.report.verified
        declaration = result.declarations[0]
        assert declaration.types == ("https://example.org/Image",)
        assert declaration.fields[0].predicate == "https://example.org/name"
        assert not view.schema.classes["Image"].slots
        """,
    ],
    ids=["shacl-conformance", "pydantic-serialization", "linkml-inheritance"],
)
def test_real_installed_integrations(
    installed_all_extras: tuple[Path, Path, dict[str, str]], program: str
) -> None:
    python, work, env = installed_all_extras
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "from pathlib import Path; import bijux_ontology; "
            "assert Path(bijux_ontology.__file__).is_relative_to("
            "Path('env').resolve())\n" + dedent(program),
        ],
        cwd=work,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_external_consumer_uses_installed_public_types(
    installed: tuple[Path, Path, dict[str, str]],
    installed_all_extras: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed
    config = work / "mypy.ini"
    config.write_text(
        "[mypy]\npython_version = 3.11\nstrict = True\n", encoding="utf-8"
    )
    source = work / "consumer.py"
    source.write_text(
        dedent("""
        from typing import assert_type
        from rdflib import Graph
        from bijux_ontology import (
            Issue, ModelDeclaration, ModelUsage, PreparedSource, Report,
            TermDeclaration, TermInfo, VocabularyView, check_emission,
            check_vocabulary, inspect_vocabulary, prepare_source,
        )
        source = prepare_source(
            "<https://example.org/Image> a <http://www.w3.org/2002/07/owl#Class> .",
            format="turtle",
        )
        assert_type(source, PreparedSource)
        vocabulary = inspect_vocabulary(source.graph())
        assert_type(vocabulary, VocabularyView)
        info = vocabulary.lookup("https://example.org/Image")
        assert_type(info, TermInfo)
        assert info.status == "declared"
        exported = source.graph()
        exported.remove((None, None, None))
        assert len(source.graph()) == 1
        report = check_vocabulary(source.graph(),
                                  [TermDeclaration("https://example.org/Image")])
        assert report.verified
        assert_type(report, Report)
        assert_type(report.issues, tuple[Issue, ...])
        assert_type(report.exit_code(), int)
        assert_type(check_emission(
            {"@context": {"@vocab": "https://example.org/"}, "@type": "Image"},
            [ModelDeclaration("image", ("https://example.org/Image",))],
            [ModelUsage("image")],
        ), Report)
        """),
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-m",
        "mypy",
        "--config-file",
        str(config),
        "--python-executable",
        str(python),
        "--no-incremental",
        "--cache-dir",
        str(work / "mypy-cache"),
        str(source),
    ]
    passed = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stdout + passed.stderr
    subprocess.run(
        [str(python), "-I", str(source)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    optional_python, optional_work, optional_env = installed_all_extras
    optional_source = optional_work / "consumer.py"
    optional_source.write_text(
        dedent("""
        from typing import Annotated, assert_type
        from pydantic import BaseModel
        from rdflib.namespace import XSD
        from bijux_ontology.interfaces.adapters.contracts import (
            ModelExtraction, RdfField,
        )
        from bijux_ontology.interfaces.adapters.pydantic import model_declarations
        from bijux_ontology.declarations import ModelDeclaration, ValueDeclaration
        from bijux_ontology.reports import Report
        class Image(BaseModel):
            name: Annotated[str, RdfField(
                "https://example.org/name",
                (ValueDeclaration("literal", str(XSD.string)),),
                output_contract=True,
            )]
        extracted = model_declarations({Image: ("https://example.org/Image",)})
        assert_type(extracted, ModelExtraction)
        assert_type(extracted.declarations, tuple[ModelDeclaration, ...])
        assert_type(extracted.report, Report)
        assert extracted.report.verified
        assert Image(name="Example").model_dump() == {"name": "Example"}
        """),
        encoding="utf-8",
    )
    optional_command = command.copy()
    optional_command[optional_command.index("--python-executable") + 1] = str(
        optional_python
    )
    optional_command[-1] = str(optional_source)
    optional_checked = subprocess.run(
        optional_command,
        cwd=optional_work,
        env=optional_env,
        capture_output=True,
        text=True,
    )
    assert optional_checked.returncode == 0, (
        optional_checked.stdout + optional_checked.stderr
    )
    subprocess.run(
        [str(optional_python), "-I", str(optional_source)],
        cwd=optional_work,
        env=optional_env,
        check=True,
        capture_output=True,
        text=True,
    )
    source.write_text(
        "from rdflib import Graph\nfrom bijux_ontology import check_vocabulary\n"
        "check_vocabulary(Graph(), [42])\n",
        encoding="utf-8",
    )
    rejected = subprocess.run(
        command, cwd=work, env=env, capture_output=True, text=True
    )
    assert rejected.returncode == 1
    assert "TermDeclaration" in rejected.stdout and "error:" in rejected.stdout


def test_core_installation_keeps_integrations_optional_and_errors_actionable(
    installed: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed
    subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            dedent("""
        from importlib.util import find_spec
        from rdflib import Graph
        from bijux_ontology.errors import ResourceError
        from bijux_ontology.interfaces.adapters.pydantic import (
            model_declarations as pydantic_models,
        )
        from bijux_ontology.interfaces.adapters.linkml import (
            model_declarations as linkml_models,
        )
        from bijux_ontology.shacl import validate_shapes
        for name in ("pyshacl", "pydantic", "linkml_runtime"):
            assert find_spec(name) is None, name
        for adapter, extra in (
            (pydantic_models, "pydantic"), (linkml_models, "linkml")
        ):
            try:
                adapter({})
            except ResourceError as error:
                assert "[" + extra + "]" in str(error)
            else:
                raise AssertionError("Missing integration falsely succeeded")
        result = validate_shapes(Graph(), Graph())
        assert result.conforms is None and result.report.exit_code() == 2
        assert "[shacl]" in result.report.issues[0].message
        """),
        ],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
