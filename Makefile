# ===========================================================================
# Nuitee Price Competitiveness Scraper - Makefile
# ===========================================================================
# Host UID/GID are exported into the build so files written to ./output are
# owned by you rather than by root.
# ===========================================================================

COMPOSE      := docker compose
SERVICE      := scraper
OUTPUT       := ./output
PYTHON       := python3

export UID   := $(shell id -u)
export GID   := $(shell id -g)

# Tunables, overridable on the command line:  make run-full-pipeline OFFSET=short
OFFSET       ?= all
DURATIONS    ?=
CITIES       ?=
LIMIT        ?=
TARGET       ?= 1000

CLI_ARGS      = --only-offset $(OFFSET)
ifneq ($(strip $(DURATIONS)),)
CLI_ARGS     += --durations "$(DURATIONS)"
endif
ifneq ($(strip $(CITIES)),)
CLI_ARGS     += --cities "$(CITIES)"
endif
ifneq ($(strip $(LIMIT)),)
CLI_ARGS     += --limit-windows $(LIMIT)
endif

.DEFAULT_GOAL := help

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
.PHONY: help
help:
	@echo ""
	@echo "  Nuitee Price Competitiveness Scraper"
	@echo "  ===================================================================="
	@echo ""
	@echo "  SETUP"
	@echo "    make build               Build the Docker image"
	@echo "    make setup               Local venv install + Chromium"
	@echo "    make input               Regenerate input.json (city list)"
	@echo "    make doctor              Pre-flight check (config, selectors, DB)"
	@echo ""
	@echo "  PIPELINE"
	@echo "    make run-full-pipeline   Both offsets, both durations, then verify"
	@echo "    make run-offset-short    Short offset only (~1 week out)"
	@echo "    make run-offset-long     Long offset only (~3 months out)"
	@echo "    make run-fallback-2m     Force the 2-month fallback offset"
	@echo "    make run-smoke           2 cities x 4 windows (fast sanity check)"
	@echo "    make plan                Print the analysis plan without scraping"
	@echo ""
	@echo "  QUALITY"
	@echo "    make verify              Repair failed tunnel + Booking rows"
	@echo "    make verify-tunnel       Repair failed tunnel extractions only"
	@echo "    make report              Print the data quality report"
	@echo "    make export              Export CSV (all rows)"
	@echo "    make export-clean        Export CSV (clean rows only)"
	@echo "    make export-parquet      Export Parquet"
	@echo ""
	@echo "  OPS"
	@echo "    make logs                Tail container logs"
	@echo "    make shell               Interactive shell in the container"
	@echo "    make stop / clean / nuke"
	@echo ""
	@echo "  DEV"
	@echo "    make test / lint / format"
	@echo ""
	@echo "  Variables: OFFSET=$(OFFSET) DURATIONS=\"$(DURATIONS)\""
	@echo "             CITIES=\"$(CITIES)\" LIMIT=$(LIMIT) TARGET=$(TARGET)"
	@echo ""

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
.PHONY: build
build:
	@echo "[build] building image..."
	$(COMPOSE) build --pull $(SERVICE)

.PHONY: setup
setup:
	@echo "[setup] installing locally..."
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m playwright install --with-deps chromium
	@[ -f .env ] || cp .env.example .env
	@echo "[setup] done. Run 'make doctor' to verify."

.PHONY: input
input:
	$(PYTHON) scripts/generate_input.py --preview-plan

.PHONY: doctor
doctor: _ensure_env _ensure_output
	$(COMPOSE) run --rm $(SERVICE) doctor

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
.PHONY: run-full-pipeline
run-full-pipeline: _ensure_env _ensure_output
	@echo "[pipeline] 1/3 scraping both offsets, both stay durations..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset all --target $(TARGET)
	@echo "[pipeline] 2/3 repairing incomplete rows..."
	$(COMPOSE) run --rm $(SERVICE) verify --mode all
	@echo "[pipeline] 3/3 exporting + reporting..."
	$(COMPOSE) run --rm $(SERVICE) export --clean-only
	$(COMPOSE) run --rm $(SERVICE) report
	@echo "[pipeline] complete. Results in $(OUTPUT)/"

.PHONY: run-offset-short
run-offset-short: _ensure_env _ensure_output
	@echo "[run] short offset (~1 week out)..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset short \
		--run-id short_$(shell date +%Y%m%d)

.PHONY: run-offset-long
run-offset-long: _ensure_env _ensure_output
	@echo "[run] long offset (~3 months out)..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset long \
		--run-id long_$(shell date +%Y%m%d)

.PHONY: run-fallback-2m
run-fallback-2m: _ensure_env _ensure_output
	@echo "[run] forcing the long offset's 2-month fallback..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset long \
		--durations "$(DURATIONS)"

.PHONY: run
run: _ensure_env _ensure_output
	$(COMPOSE) run --rm $(SERVICE) run $(CLI_ARGS)

.PHONY: run-smoke
run-smoke: _ensure_env _ensure_output
	@echo "[smoke] 2 cities, 4 windows, 5 hotels each..."
	$(COMPOSE) run --rm $(SERVICE) run \
		--cities "Casablanca,Marrakech" --only-offset all \
		--limit-windows 4 --hotels-per-city 5 \
		--run-id smoke_$(shell date +%Y%m%d_%H%M%S)

.PHONY: start
start: _ensure_env _ensure_output
	$(COMPOSE) up -d $(SERVICE)
	$(COMPOSE) logs -f $(SERVICE)

.PHONY: plan
plan:
	$(PYTHON) -m src.cli plan --only-offset $(OFFSET) --dry-run

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
.PHONY: verify
verify: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) verify --mode all --export

.PHONY: verify-tunnel
verify-tunnel: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) verify --mode tunnel $(if $(LIMIT),--limit $(LIMIT),)

.PHONY: verify-booking
verify-booking: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) verify --mode booking $(if $(LIMIT),--limit $(LIMIT),)

.PHONY: verify-dry
verify-dry: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) verify --mode all --dry-run

.PHONY: report
report: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) report

.PHONY: export
export: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) export

.PHONY: export-clean
export-clean: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) export --clean-only

.PHONY: export-parquet
export-parquet: _ensure_output
	$(COMPOSE) run --rm $(SERVICE) export --parquet

# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
.PHONY: logs
logs:
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

.PHONY: shell
shell:
	$(COMPOSE) run --rm --entrypoint bash $(SERVICE)

.PHONY: stop
stop:
	-$(COMPOSE) stop $(SERVICE)

.PHONY: clean
clean: stop
	-$(COMPOSE) rm -f $(SERVICE)
	@echo "[clean] clearing $(OUTPUT) (database preserved)..."
	@find $(OUTPUT) -mindepth 1 ! -name '.gitkeep' ! -name '*.db' -delete 2>/dev/null || true

.PHONY: clean-db
clean-db:
	@echo "[clean-db] DELETING the scraped dataset."
	@read -p "Type 'yes' to confirm: " ok; [ "$$ok" = "yes" ] || exit 1
	@rm -f $(OUTPUT)/*.db $(OUTPUT)/*.db-wal $(OUTPUT)/*.db-shm

.PHONY: nuke
nuke: clean
	-$(COMPOSE) down --rmi local --volumes --remove-orphans

# ---------------------------------------------------------------------------
# Dev
# ---------------------------------------------------------------------------
.PHONY: test
test:
	$(PYTHON) -m pytest tests/ -v

.PHONY: test-cov
test-cov:
	$(PYTHON) -m pytest tests/ --cov=src --cov-report=term-missing

.PHONY: lint
lint:
	$(PYTHON) -m ruff check src/ tests/ scripts/

.PHONY: format
format:
	$(PYTHON) -m ruff format src/ tests/ scripts/

# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------
.PHONY: _ensure_env
_ensure_env:
	@if [ ! -f .env ]; then \
		echo "[error] .env not found. Run: cp .env.example .env"; \
		exit 1; \
	fi

.PHONY: _ensure_output
_ensure_output:
	@mkdir -p $(OUTPUT)
