BIJUX_REPOSITORY_ENV_OVERLAY_INCLUDED := 1
MONOREPO_ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/..)
ROOT_MAKE_DIR := $(MONOREPO_ROOT)/makes
CONFIG_DIR := $(MONOREPO_ROOT)/configs
include $(ROOT_MAKE_DIR)/bijux-py/repository/env.mk
