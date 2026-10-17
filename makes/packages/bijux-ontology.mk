PACKAGE_KIND := repository-python
PACKAGE_IMPORT_NAME := bijux_ontology
PACKAGE_DEFINE_VENV := 0
PACKAGE_DEFINE_INSTALL := 0
PACKAGE_INSTALL_TARGETS :=
PACKAGE_BOOTSTRAP_TARGETS :=
LINT_PRE_TARGETS := setup
QUALITY_PRE_TARGETS := setup
TEST_PRE_TARGETS := setup
BUILD_PRE_TARGETS := setup
LINT_DIRS := src tests
QUALITY_PATHS := src
QUALITY_MYPY_TARGETS := src
QUALITY_MYPY_CONFIG = $(MONOREPO_ROOT)/configs/mypy.ini
DEPTRY_CONFIG = $(PROJECT_DIR)/pyproject.toml
CODESPELL = $(VENV_PYTHON) -m codespell_lib
TEST_PATHS := tests
TEST_PATHS_UNIT := tests/unit
TEST_PATHS_E2E :=
TEST_CI_TARGETS := test
TEST_E2E_ARGS := --maxfail=1 -q
PYTEST_CONFIG = $(MONOREPO_ROOT)/pyproject.toml
COVERAGE_CONFIG = $(MONOREPO_ROOT)/configs/coveragerc.ini
ENABLE_BENCH := 0
BUILD_TOOLS_COMMAND = $(VENV_PYTHON) -c 'import build, twine'
BUILD_TEMP_CLEAN_PATHS :=
include $(abspath $(dir $(firstword $(MAKEFILE_LIST))))/../bijux-py/package.mk
