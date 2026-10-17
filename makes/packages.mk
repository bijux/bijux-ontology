ROOT_PACKAGE_PROFILE_DIR := $(ROOT_MAKEFILE_DIR)/packages
ROOT_PACKAGE_TARGETS := test test-unit test-e2e lint quality build clean
ROOT_TARGET_PACKAGES_test-e2e := bijux-ontology-dev
ROOT_TARGET_GROUPS_test-unit := test
ROOT_TARGET_GROUPS_test-e2e := test
ROOT_TARGET_SHARED_ENV_test-unit := 1
ROOT_TARGET_SHARED_ENV_test-e2e := 1
ROOT_TARGET_SHARED_ENV_build := 1
PACKAGE_RECORDS := \
	bijux-ontology|primary,check,buildable,test|bijux-ontology.mk \
	bijux-ontology-dev|check,buildable,test|bijux-ontology-dev.mk
include $(ROOT_MAKEFILE_DIR)/bijux-py/package-catalog.mk
