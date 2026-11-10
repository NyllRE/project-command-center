PLUGIN_ID := project-command-center
LIVE_DIR := $(HOME)/.hermes/desktop-plugins/$(PLUGIN_ID)

# The suites need the Hermes venv (fastapi and the hermes packages live there).
# Override with `make test PYTHON=/path/to/python`.
PYTHON ?= $(shell for p in "$$HERMES_PYTHON" "$$HOME/.hermes/hermes-agent/venv/bin/python"; do \
	if [ -n "$$p" ] && [ -x "$$p" ]; then echo "$$p"; break; fi; done)

PY_SUITES := test_hooks test_session_status test_git_routes test_branch_switch test_icons test_avatars test_github_states

.PHONY: dev validate test test-py test-js

dev:
	mkdir -p $(LIVE_DIR)
	cp desktop/plugin.js $(LIVE_DIR)/plugin.js
	@echo "copied to $(LIVE_DIR)/plugin.js"

validate:
	hermes plugins validate .

test: test-py test-js

test-py:
	@test -n "$(PYTHON)" || { echo "No Hermes python found. Set PYTHON=/path/to/python"; exit 1; }
	@echo "python: $(PYTHON)"
	@set -e; for suite in $(PY_SUITES); do printf "%-22s " "$$suite"; $(PYTHON) tests/$$suite.py >/tmp/pcc-$$suite.log 2>&1 && echo ok || { echo FAILED; tail -20 /tmp/pcc-$$suite.log; exit 1; }; done

test-js:
	@node --test tests/graph_layout.test.mjs tests/github_status.test.mjs tests/session_lane.test.mjs 
