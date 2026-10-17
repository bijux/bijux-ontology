.PHONY: bijux-std-projection standards-refresh
bijux-std-checks: bijux-std-projection

bijux-std-projection: ## Verify accepted verifier inputs and Python Make projections
	@PYTHONPATH="$(MONOREPO_ROOT)/packages/bijux-ontology-dev/src" $(PYTHON) -m bijux_ontology_dev.standards.projection --repo-root "$(MONOREPO_ROOT)"

standards-refresh: bijux-std-update ## Refresh verifier inputs after selecting an accepted standard pin
	@PYTHONPATH="$(MONOREPO_ROOT)/packages/bijux-ontology-dev/src" $(PYTHON) -m bijux_ontology_dev.standards.projection --repo-root "$(MONOREPO_ROOT)" --refresh
