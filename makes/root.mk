ROOT_MAKEFILE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PROJECT_ARTIFACTS_DIR := $(abspath artifacts)
include $(ROOT_MAKEFILE_DIR)/bijux-py/root/env.mk
PROJECT_PROCESS_DIR := $(ROOT_PROCESS_DIR)
include $(ROOT_MAKEFILE_DIR)/env.mk
include $(ROOT_MAKEFILE_DIR)/packages.mk

ROOT_ALL_TARGETS := lint quality test-ci build
ROOT_CHECK_ENV_COMMAND = @$(UV_SYNC) >/dev/null
include $(ROOT_MAKEFILE_DIR)/bijux-py/repository/root.mk
include $(ROOT_MAKEFILE_DIR)/bijux-py/root/package-dispatch.mk
include $(ROOT_MAKEFILE_DIR)/bijux-std.mk
include $(ROOT_MAKEFILE_DIR)/standards.mk
MAKE_LAYOUT_REQUIRED_FILES := env.mk packages.mk publish.mk root.mk standards.mk
CONFIG_LAYOUT_REQUIRED_FILES := coveragerc.ini mypy.ini ruff.toml
include $(ROOT_MAKEFILE_DIR)/bijux-py/repository/make-layout.mk
include $(ROOT_MAKEFILE_DIR)/bijux-py/repository/config-layout.mk
include $(ROOT_MAKEFILE_DIR)/bijux-py/ci/help.mk
BIJUX_STD_CHECK_SCRIPT := .bijux/shared/bijux-checks/check-bijux-std.sh
BIJUX_STD_UPDATE_SCRIPT := .bijux/shared/bijux-checks/update-bijux-std.sh
BIJUX_STD_REF := $(shell cat .github/standards/bijux-std.sha)
export BIJUX_STD_CAPABILITIES := common python
export BIJUX_STD_STRICT_REMOTE := 1
export BIJUX_STD_REQUIRE_REMOTE_MATCH := 1

.PHONY: test-ci check
help: ## Show repository and package commands
test-ci: test ## Run all unit, functional and installed-package tests
check: lock-check check-python-baseline check-make-layout check-config-layout bijux-std-checks lint quality test-ci build ## Verify repository tooling and the complete deliverable

api security sbom docs docs-check docs-serve:
	@echo "The '$@' gate is not configured; make check runs the supported verification contract." >&2
	@exit 2
