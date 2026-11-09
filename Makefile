PLUGIN_ID := project-command-center
LIVE_DIR := $(HOME)/.hermes/desktop-plugins/$(PLUGIN_ID)

.PHONY: dev validate

dev:
	mkdir -p $(LIVE_DIR)
	cp desktop/plugin.js $(LIVE_DIR)/plugin.js
	@echo "copied to $(LIVE_DIR)/plugin.js"

validate:
	hermes plugins validate . 
