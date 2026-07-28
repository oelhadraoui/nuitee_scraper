"""
src/main.py
=============================================================================
Backwards-compatible entry point.

The original deliverable was invoked as ``python src/main.py``, and the
Dockerfile / docker-compose / cron jobs of the existing deployment all call it
that way. Rather than break them, this module forwards to the new CLI.

    python src/main.py                       -> full run with defaults
    python src/main.py --horizon 3 --months 2
    python src/main.py run --horizon 7
    python src/main.py doctor

Anything that does not start with a known subcommand is treated as arguments
to ``run``, which preserves the legacy ``--input input.json`` invocation.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python src/main.py` (as opposed to `python -m src.main`) to resolve
# the `src` package by putting the project root on sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.cli import main as cli_main  # noqa: E402

_SUBCOMMANDS = {"run", "plan", "verify", "report", "export", "doctor"}


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] not in _SUBCOMMANDS:
        # Legacy invocation - default to `run`.
        argv = ["run", *argv]
    cli_main(argv)


if __name__ == "__main__":
    main()
