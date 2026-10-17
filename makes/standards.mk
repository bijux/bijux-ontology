.PHONY: bijux-std-projection standards-refresh
bijux-std-checks: bijux-std-projection

bijux-std-projection: ## Verify accepted verifier inputs and Python Make projections
	@$(PYTHON) $(MONOREPO_ROOT)/scripts/standards/projection.py

standards-refresh: bijux-std-update ## Refresh verifier inputs after selecting an accepted standard pin
	@$(PYTHON) $(MONOREPO_ROOT)/scripts/standards/projection.py --refresh
