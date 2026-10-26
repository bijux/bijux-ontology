"""Qualify the optional wheel's public types from an ordinary strict consumer."""

import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def test_installed_reasoning_types_are_checked_without_starting_native_work(
    installed_reasoning: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, env = installed_reasoning
    source = work / "consumer.py"
    source.write_text(
        dedent("""
        import sys
        from pathlib import Path
        from typing import assert_type

        def audit(event: str, arguments: tuple[object, ...]) -> None:
            assert event != "subprocess.Popen", "Unexpected native work"

        sys.addaudithook(audit)
        import bijux_ontology_reasoning
        from bijux_ontology.diagnostics.errors import InputError
        from bijux_ontology.declarations.types import ModelDeclaration
        from bijux_ontology.rdf.sources.snapshots import PreparedSource
        from bijux_ontology_reasoning.backend.owlapi.bundles import (
            prepare_profile_backend,
        )
        from bijux_ontology_reasoning.backend.types import OwlProfileBackend
        from bijux_ontology_reasoning.inference.model_contracts.checking import (
            check_model_compatibility,
        )
        from bijux_ontology_reasoning.inference.model_contracts.types import (
            ModelCompatibility, ModelQuestion, ModelQuestionKind,
        )
        from bijux_ontology_reasoning.theories.types import TheoryCombination

        question = ModelQuestion("record", "required_subsumption", "record")
        assert_type(question.kind, ModelQuestionKind)
        combination = TheoryCombination("deployment", ("application",))
        assert_type(combination.components, tuple[str, ...])

        def check(
            backend: OwlProfileBackend, source: PreparedSource,
        ) -> ModelCompatibility:
            result = check_model_compatibility(
                backend, source, (ModelDeclaration("record", ("urn:Record",)),),
            )
            assert_type(result, ModelCompatibility)
            return result

        try:
            prepare_profile_backend(
                Path("java"), Path("bundle"), manifest_sha256="0" * 64,
            )
        except InputError:
            pass
        else:
            raise AssertionError("Relative Java path was accepted")
        assert bijux_ontology_reasoning.__file__ is not None
        assert Path(bijux_ontology_reasoning.__file__).is_relative_to(
            Path(sys.prefix).resolve(),
        )
        """),
        encoding="utf-8",
    )
    config = work / "mypy.ini"
    config.write_text("[mypy]\nstrict = True\n", encoding="utf-8")
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
    result = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    subprocess.run(
        [str(python), "-I", str(source)],
        cwd=work,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    source.write_text(
        "from bijux_ontology_reasoning.inference.model_contracts.types import (\n"
        "    ModelQuestion,\n)\n"
        'ModelQuestion(model="record", kind="not-a-logical-question",\n'
        '              location="record", branch="first")\n',
        encoding="utf-8",
    )
    rejected = subprocess.run(
        command, cwd=work, env=env, capture_output=True, text=True
    )
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    assert '"kind"' in rejected.stdout and '"branch"' in rejected.stdout
