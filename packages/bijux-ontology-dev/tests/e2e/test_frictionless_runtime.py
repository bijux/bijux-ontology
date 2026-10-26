"""Actual separate native package runtimes coexist with an installed OAK host."""

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest


@pytest.mark.timeout(300)
def test_oak_host_uses_explicit_native_package_interpreter(
    installed_oak: tuple[Path, Path, dict[str, str]],
    installed_frictionless: tuple[Path, Path, dict[str, str]],
    installed: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, environment = installed_oak
    foreign, _, _ = installed_frictionless
    core, _, _ = installed
    program = r"""
        import importlib.util
        import sys
        import threading
        from pathlib import Path
        import oaklib
        import bijux_ontology
        from rdflib.namespace import XSD
        from bijux_ontology.declarations.types import FieldDeclaration, ValueDeclaration
        from bijux_ontology.diagnostics.errors import ResourceError
        from bijux_ontology.interfaces.adapters.frictionless import (
            ColumnBinding, ResourceBinding, extract_package,
            prepare_frictionless_backend,
        )
        assert Path(bijux_ontology.__file__).is_relative_to(Path('env').resolve())
        assert importlib.util.find_spec('frictionless') is None
        directory = Path('package-work').resolve()
        directory.mkdir()
        backend = prepare_frictionless_backend(
            Path(sys.argv[1]), work_directory=directory)
        assert backend.python == Path(sys.argv[1])
        description = {'resources': [
            {'name': 'lookup', 'path': 'lookup.csv', 'schema': {
                'fields': [{'name': 'code', 'type': 'string'}], 'primaryKey': 'code'}},
            {'name': 'data', 'path': 'data.csv', 'schema': {
                'fields': [{'name': 'id', 'type': 'string'},
                           {'name': 'code', 'type': 'string'},
                           {'name': 'value', 'type': 'number'}],
                'foreignKeys': [{'fields': 'code', 'reference': {
                    'resource': 'lookup', 'fields': 'code'}}]}}
        ]}
        resources = {'lookup.csv': b'code\nc1\n',
                     'data.csv': b'id,code,value\nurn:row:1,c1,1.250\n'}
        bindings = (ResourceBinding('data', 'id', (ColumnBinding(FieldDeclaration(
            'value', 'urn:value', (ValueDeclaration('literal', str(XSD.decimal)),)
        )),)),)
        valid = extract_package(description, resources, bindings,
                                work_directory=directory, backend=backend)
        assert valid.report.verified, valid.report.to_dict()
        assert valid.producer.python == backend.python_version
        resources['data.csv'] = resources['data.csv'].replace(b'c1', b'absent')
        invalid = extract_package(description, resources, bindings,
                                  work_directory=directory, backend=backend)
        assert not invalid.report.verified and invalid.source is None
        assert any(issue.code == 'frictionless.native_foreign_key'
                   for issue in invalid.report.issues)
        try:
            prepare_frictionless_backend(Path(sys.argv[2]), work_directory=directory)
        except ResourceError:
            pass
        else:
            raise AssertionError('Core-only interpreter admitted as native runtime')
        for options in ({'timeout': 0.02}, {'cancel': threading.Event()}):
            if 'cancel' in options:
                options['cancel'].set()
            interrupted = extract_package(description, resources, bindings,
                work_directory=directory, backend=backend, **options)
            assert not interrupted.report.verified and interrupted.source is None
            assert not interrupted.report.complete
            assert not list(directory.iterdir())
        assert not list(directory.iterdir())
        import subprocess
        located = subprocess.run([str(backend.python), '-I', '-B', '-c',
            'import bijux_ontology.interfaces.adapters.frictionless.identity as i; '
            'print(i.__file__)'], check=True, text=True, capture_output=True)
        identity = Path(located.stdout.strip())
        original = identity.read_bytes()
        try:
            # Isolate this interpreter from shared installer cache hard links.
            identity.unlink()
            identity.write_bytes(original + b'\n')
            mismatched = extract_package(description, resources, bindings,
                work_directory=directory, backend=backend)
            assert not mismatched.report.verified and mismatched.source is None
            assert any('identity differs' in issue.message
                       for issue in mismatched.report.issues)
            try:
                prepare_frictionless_backend(backend.python, work_directory=directory)
            except ResourceError:
                pass
            else:
                raise AssertionError('Different worker accepted as this distribution')
        finally:
            identity.write_bytes(original)
        assert not list(directory.iterdir())
        assert 'frictionless' not in sys.modules
    """
    result = subprocess.run(
        [str(python), "-I", "-B", "-c", dedent(program), str(foreign), str(core)],
        cwd=work,
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
