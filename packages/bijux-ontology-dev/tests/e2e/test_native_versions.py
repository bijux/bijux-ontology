"""Installed maintained APIs retain actual numerical, lexical and native semantics."""

import subprocess
from pathlib import Path

import pytest


@pytest.mark.timeout(600)
def test_supported_native_releases_execute_original_feature_contracts(
    installed_native_versions: tuple[Path, Path, dict[str, str]],
    distribution: tuple[Path, Path, dict[str, str]],
    adapters_distribution: tuple[Path, Path, dict[str, str]],
) -> None:
    python, work, environment = installed_native_versions
    _, source, _ = distribution
    tests = source / "tests/functional"
    adapter_tests = adapters_distribution[1] / "tests/functional"
    cases = (
        (
            tests / "data/test_analysis.py",
            "test_enrichment_matches_direct_native_fixed_family_and_changed_background",
        ),
        (
            adapter_tests / "arrow/test_records.py",
            "test_real_two_batches_match_rdf_oracle_and_locate_second_batch_bad_term",
        ),
        (
            adapter_tests / "arrow/test_records.py",
            "test_declared_null_nan_and_no_guessed_output_types",
        ),
        (
            adapter_tests / "biolink/test_checking.py",
            "test_real_official_qualified_example_preserves_full_statement",
        ),
        (
            adapter_tests / "biolink/test_checking.py",
            "test_qualifier_predicate_and_endpoint_mutations_are_not_hidden",
        ),
        (
            adapter_tests / "biolink/test_checking.py",
            "test_actual_published_releases_change_the_same_qualifier_context",
        ),
        (
            tests / "shex/test_validation.py",
            "test_recursive_native_results_and_changed_descendant",
        ),
        (
            tests / "shex/test_validation.py",
            "test_repeated_exact_terms_native_datatypes_and_capacity",
        ),
    )
    result = subprocess.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pytest",
            "-c",
            "/dev/null",
            "-q",
            "--import-mode=importlib",
            "--timeout=120",
            "--basetemp=" + str(work / "native-results"),
            "-o",
            "cache_dir=" + str(work / "pytest-cache"),
            *(str(path) + "::" + selector for path, selector in cases),
        ],
        cwd=work,
        env=environment,
        text=True,
        capture_output=True,
        timeout=500,
    )
    (work / "native-tests.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 passed" in result.stdout and "skipped" not in result.stdout
