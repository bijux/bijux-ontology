BIJUX_REPOSITORY_ENV_OVERLAY_INCLUDED := 1
MONOREPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ROOT_MAKE_DIR := $(MONOREPO_ROOT)/makes
CONFIG_DIR := $(MONOREPO_ROOT)/configs
include $(ROOT_MAKE_DIR)/bijux-py/repository/env.mk
CODESPELL = $(VENV_PYTHON) -m codespell_lib --config "$(CONFIG_DIR)/codespell.ini"
VULTURE = $(VENV_PYTHON) -m vulture --config "$(CONFIG_DIR)/vulture.toml"
QUALITY_INTERROGATE_FLAGS := --config "$(CONFIG_DIR)/interrogate.toml"
QUALITY_DEPTRY_COMMAND = $(VENV_PYTHON) -m bijux_ontology_dev.quality.dependencies --config "$(DEPTRY_CONFIG)" --project-dir "$(PROJECT_DIR)" --output-dir "$(QUALITY_ARTIFACTS_DIR)"
