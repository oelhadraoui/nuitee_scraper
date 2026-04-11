# ---------------------------------------------------------------------------
# Hotel Price Scraper — Makefile
# ---------------------------------------------------------------------------
# Passes the current user's UID/GID into the build so output files are
# owned by you, not root.
# ---------------------------------------------------------------------------

COMPOSE  := docker compose
SERVICE  := scraper
OUTPUT   := ./output

# Export host UID/GID so docker-compose.yml can consume them
export UID := $(shell id -u)
export GID := $(shell id -g)

.DEFAULT_GOAL := help

# ── Help ────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "  Hotel Price Scraper — available commands"
	@echo "  ─────────────────────────────────────────"
	@echo "  make build     Build (or rebuild) the Docker image"
	@echo "  make start     Run the scraper in the background"
	@echo "  make run       Run the scraper in the foreground (shows live logs)"
	@echo "  make stop      Stop the running container"
	@echo "  make logs      Tail container logs  (Ctrl-C to exit)"
	@echo "  make shell     Open a bash shell inside the container"
	@echo "  make clean     Stop containers + wipe temporary output files"
	@echo "  make nuke      clean + remove Docker image layers"
	@echo ""

# ── Build ────────────────────────────────────────────────────────────────
.PHONY: build
build:
	@echo "[build] Building Docker image…"
	$(COMPOSE) build --pull $(SERVICE)

# ── Start (detached) ─────────────────────────────────────────────────────
.PHONY: start
start: _ensure_env _ensure_output
	@echo "[start] Starting scraper in background…"
	$(COMPOSE) up -d $(SERVICE)
	@echo "[start] Tailing logs (Ctrl-C safe — container keeps running):"
	$(COMPOSE) logs -f $(SERVICE)

# ── Run (foreground — exits when scraper finishes) ───────────────────────
.PHONY: run
run: _ensure_env _ensure_output
	@echo "[run] Running scraper in foreground…"
	$(COMPOSE) run --rm $(SERVICE)

# ── Stop ─────────────────────────────────────────────────────────────────
.PHONY: stop
stop:
	@echo "[stop] Stopping container…"
	$(COMPOSE) stop $(SERVICE)

# ── Logs ─────────────────────────────────────────────────────────────────
.PHONY: logs
logs:
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

# ── Interactive shell ────────────────────────────────────────────────────
.PHONY: shell
shell:
	$(COMPOSE) run --rm --entrypoint bash $(SERVICE)

# ── Clean ────────────────────────────────────────────────────────────────
.PHONY: clean
clean: stop
	@echo "[clean] Removing containers…"
	$(COMPOSE) rm -f $(SERVICE)
	@echo "[clean] Clearing output folder (keeping directory)…"
	@find $(OUTPUT) -mindepth 1 ! -name '.gitkeep' -delete 2>/dev/null || true
	@echo "[clean] Done."

# ── Nuke ─────────────────────────────────────────────────────────────────
.PHONY: nuke
nuke: clean
	@echo "[nuke] Removing Docker image layers…"
	$(COMPOSE) down --rmi local --volumes --remove-orphans
	@echo "[nuke] Done."

# ── Internal helpers ─────────────────────────────────────────────────────
.PHONY: _ensure_env
_ensure_env:
	@if [ ! -f .env ]; then \
		echo "[error] .env file not found — copy .env.example and fill it in."; \
		exit 1; \
	fi

.PHONY: _ensure_output
_ensure_output:
	@mkdir -p $(OUTPUT)