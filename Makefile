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
TARGET       ?= 700

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
	@echo "    make doctor              Pre-flight check (config, selectors, storage)"
	@echo ""
	@echo "  PIPELINE"
	@echo "    make run-full-pipeline   Both offsets, both durations, then verify"
	@echo "    make run-offset-short    Short offset only (~1 week out)"
	@echo "    make run-offset-long     Long offset only (~$(HORIZON_LONG_OFFSET_MONTHS) out)"
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
doctor: _ensure_env _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) doctor

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
.PHONY: run-full-pipeline
run-full-pipeline: _ensure_env _ensure_output _ensure_input
	@echo "[pipeline] 1/3 scraping both offsets, both stay durations..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset all --target $(TARGET)
	@echo "[pipeline] 2/3 repairing incomplete rows..."
	$(COMPOSE) run --rm $(SERVICE) verify --mode all
	@echo "[pipeline] 3/3 exporting + reporting..."
	$(COMPOSE) run --rm $(SERVICE) export --clean-only
	$(COMPOSE) run --rm $(SERVICE) report
	@echo "[pipeline] complete. Results in $(OUTPUT)/"

.PHONY: run-offset-short
run-offset-short: _ensure_env _ensure_output _ensure_input
	@echo "[run] short offset (~1 week out)..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset short \
		--run-id short_$(shell date +%Y%m%d)

.PHONY: run-offset-long
run-offset-long: _ensure_env _ensure_output _ensure_input
	@echo "[run] long offset (~$(HORIZON_LONG_OFFSET_MONTHS) out)..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset long \
		--run-id long_$(shell date +%Y%m%d)

.PHONY: run-fallback-2m
run-fallback-2m: _ensure_env _ensure_output _ensure_input
	@echo "[run] forcing the long offset's 2-month fallback..."
	$(COMPOSE) run --rm $(SERVICE) run --only-offset long \
		--durations "$(DURATIONS)"

.PHONY: run
run: _ensure_env _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) run $(CLI_ARGS)

.PHONY: run-smoke
run-smoke: _ensure_env _ensure_output _ensure_input
	@echo "[smoke] 2 cities, 4 windows, 5 hotels each..."
	$(COMPOSE) run --rm $(SERVICE) run \
		--cities "Casablanca,Marrakech" --only-offset all \
		--limit-windows 4 --hotels-per-city 5 \
		--run-id smoke_$(shell date +%Y%m%d_%H%M%S)

.PHONY: start
start: _ensure_env _ensure_output _ensure_input
	$(COMPOSE) up -d $(SERVICE)
	$(COMPOSE) logs -f $(SERVICE)

.PHONY: plan
plan:
	$(PYTHON) -m src.cli plan --only-offset $(OFFSET) --dry-run

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------
.PHONY: verify
verify: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) verify --mode all --export

.PHONY: verify-tunnel
verify-tunnel: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) verify --mode tunnel $(if $(LIMIT),--limit $(LIMIT),)

.PHONY: verify-booking
verify-booking: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) verify --mode booking $(if $(LIMIT),--limit $(LIMIT),)

.PHONY: verify-dry
verify-dry: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) verify --mode all --dry-run

.PHONY: report
report: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) report

.PHONY: export
export: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) export

.PHONY: export-clean
export-clean: _ensure_output _ensure_input
	$(COMPOSE) run --rm $(SERVICE) export --clean-only

.PHONY: export-parquet
export-parquet: _ensure_output _ensure_input
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
	@echo "[clean] clearing $(OUTPUT) (prices.csv / prices_clean.csv preserved)..."
	@find $(OUTPUT) -mindepth 1 ! -name '.gitkeep' ! -name '*.csv' -delete 2>/dev/null || true

.PHONY: clean-data
clean-data:
	@echo "[clean-data] DELETING the scraped dataset (prices.csv, prices_clean.csv)."
	@read -p "Type 'yes' to confirm: " ok; [ "$$ok" = "yes" ] || exit 1
	@rm -f $(OUTPUT)/prices.csv $(OUTPUT)/prices_clean.csv

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
	@# The container writes here as the "scraper" user, whose UID/GID were
	@# baked into the image the last time it was BUILT (see docker-compose.yml's
	@# build.args: UID/GID). If that build happened under a different UID than
	@# whoever is running `make` right now (built via sudo, built by a teammate,
	@# a stale cached image from before your shell exported UID/GID, WSL/Docker
	@# Desktop UID remapping, ...), a freshly-created ./output is owned by YOU
	@# but the container's internal user still can't write into it - the classic
	@# first-run "PermissionError: [Errno 13] ... /app/output/scraper.log".
	@# Rather than chase every environment's UID alignment, just make sure the
	@# mount is writable by anyone; this is what fixes it if you've ever had to
	@# manually `chmod 777 output` to get `make doctor` working.
	@chmod -R 777 $(OUTPUT) 2>/dev/null || true

.PHONY: _ensure_input
_ensure_input:
	@# docker-compose.yml bind-mounts input_morocco.json / input_international.json
	@# by name (see README §3). A bind mount's SOURCE has to exist on the host
	@# before Compose starts the container, or the mount point gets filled in
	@# with an empty directory instead of the real file - which is exactly what
	@# produces "Input file not found: input_international.json" even when the
	@# file is sitting right there: it existed a moment too late for THIS
	@# particular container start. Generating both up front, once, idempotently,
	@# means every `make` target always has real files to mount, whether or
	@# not this particular run intends to use them.
	@test -f input_morocco.json || \
		$(PYTHON) scripts/generate_input.py --preset morocco --output input_morocco.json >/dev/null
	@test -f input_international.json || \
		$(PYTHON) scripts/generate_input.py --preset europe --preset mena --preset world \
			--output input_international.json >/dev/null
