MONOREPO_ROOT := $(CURDIR)
PROJECT_DIR := $(CURDIR)
PROJECT_SLUG := bijux-ontology
PROJECT_ARTIFACTS_DIR := $(CURDIR)/artifacts/checks
CONFIG_DIR := $(CURDIR)/configs
PYTHON ?= python3.11
UV := uv
VENV_PYTHON ?= $(CURDIR)/artifacts/env/bin/python
SELF_MAKE := $(MAKE) --no-print-directory
LINT_DIRS := src tests
QUALITY_PATHS := src
QUALITY_MYPY_CONFIG := $(CONFIG_DIR)/mypy.ini
QUALITY_MYPY_TARGETS := src
DEPTRY_CONFIG := $(PROJECT_DIR)/pyproject.toml
CODESPELL := $(dir $(VENV_PYTHON))codespell
TEST_PATHS := tests
TEST_PATHS_UNIT := tests/unit
TEST_PATHS_E2E := tests/e2e
TEST_CI_TARGETS := test
TEST_E2E_ARGS := --maxfail=1 -q
TEST_PYTHON := $(VENV_PYTHON)
PYTEST_CONFIG := $(CURDIR)/pyproject.toml
COVERAGE_CONFIG := $(CONFIG_DIR)/coveragerc.ini
ENABLE_BENCH := 0
BUILD_TOOLS_COMMAND := $(VENV_PYTHON) -c 'import build, twine'
BUILD_TEMP_CLEAN_PATHS :=
ROOT_DEFINE_CLEAN := 0
ROOT_ALL_TARGETS := lint quality test-ci build
ROOT_INSTALL_COMMAND := $(UV) sync --frozen --python "$(PYTHON)" --all-groups --all-extras
BIJUX_CI_PR_TARGETS := lint quality test-ci build
BIJUX_STD_CHECK_SCRIPT := .bijux/shared/bijux-checks/check-bijux-std.sh
BIJUX_STD_UPDATE_SCRIPT := .bijux/shared/bijux-checks/update-bijux-std.sh
BIJUX_STD_REF := $(shell cat .github/standards/bijux-std.sha)
export BIJUX_STD_CAPABILITIES := common python
export BIJUX_STD_STRICT_REMOTE := 1
export BIJUX_STD_REQUIRE_REMOTE_MATCH := 1
export UV_CACHE_DIR := $(CURDIR)/artifacts/cache/uv
export UV_PROJECT_ENVIRONMENT := $(CURDIR)/artifacts/env
export PYTHONDONTWRITEBYTECODE := 1

include .bijux/shared/bijux-makes/bijux.mk
include .bijux/shared/bijux-makes-py/ci/lint.mk
include .bijux/shared/bijux-makes-py/ci/quality.mk
include .bijux/shared/bijux-makes-py/ci/test.mk
include .bijux/shared/bijux-makes-py/ci/build.mk
include .bijux/shared/bijux-makes-py/root/lifecycle.mk
include makes/bijux-std.mk
include makes/standards.mk
